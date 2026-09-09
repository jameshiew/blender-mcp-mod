use std::sync::Arc;

use anyhow::{Context, Result, bail, ensure};
use base64::{Engine, prelude::BASE64_STANDARD};
use rmcp::model::{CallToolResult, ContentBlock, Tool};
use serde_json::{Map, Value, json};

use crate::{addon::PROTOCOL_VERSION, connection::BlenderConnection};

pub struct ToolDefinition {
    pub tool: Tool,
    validator: jsonschema::Validator,
}

pub fn definitions() -> Result<Vec<ToolDefinition>> {
    let tools: Vec<Tool> = serde_json::from_str(include_str!("../resources/tools.json"))?;
    tools
        .into_iter()
        .map(|tool| {
            let validator =
                jsonschema::validator_for(&Value::Object((*tool.input_schema).clone()))?;
            Ok(ToolDefinition { tool, validator })
        })
        .collect()
}

impl ToolDefinition {
    pub fn arguments(&self, mut args: Map<String, Value>) -> Result<Map<String, Value>> {
        // Older clients may still send this removed, collection-only argument.
        args.remove("user_prompt");
        self.validator
            .validate(&Value::Object(args.clone()))
            .map_err(|error| anyhow::anyhow!("Invalid arguments: {error}"))?;
        if let Some(properties) = self
            .tool
            .input_schema
            .get("properties")
            .and_then(Value::as_object)
        {
            for (name, schema) in properties {
                if let Some(default) = schema.get("default") {
                    args.entry(name.clone()).or_insert_with(|| default.clone());
                }
            }
        }
        Ok(args)
    }
}

pub async fn prepare(name: &str, mut args: Map<String, Value>) -> Result<(String, Value)> {
    let command = match name {
        "get_addon_status" => "get_addon_info",
        "get_object_info" => {
            let value = args.remove("object_name").context("Missing object_name")?;
            args.insert("name".into(), value);
            name
        }
        "execute_blender_code" => "execute_code",
        "download_sketchfab_model" => {
            args.insert("normalize_size".into(), json!(true));
            name
        }
        _ => name,
    };
    Ok((command.into(), Value::Object(args)))
}

pub async fn execute(
    connection: &Arc<BlenderConnection>,
    name: &str,
    args: Map<String, Value>,
) -> Result<CallToolResult> {
    let (command, params) = prepare(name, args).await?;
    let mut result = connection.send(&command, params).await?;
    if name == "get_viewport_screenshot" || name == "get_sketchfab_model_preview" {
        let data = result["image_data"].as_str().context(
            "Add-on did not return image data; run blender-mcp install-addon and restart Blender",
        )?;
        BASE64_STANDARD
            .decode(data)
            .context("Invalid image data from Blender")?;
        let mime = match result["format"].as_str().unwrap_or("jpeg") {
            "png" => "image/png",
            "jpeg" | "jpg" => "image/jpeg",
            "webp" => "image/webp",
            other => bail!("Unsupported image format: {other}"),
        };
        let image = ContentBlock::image(data, mime);
        result
            .as_object_mut()
            .context("Invalid image response")?
            .remove("image_data");
        let mut response = CallToolResult::structured(result);
        response.content.insert(0, image);
        return Ok(response);
    }
    if name == "get_addon_status" {
        ensure!(result.is_object(), "Invalid add-on information");
        result["expected_protocol_version"] = json!(PROTOCOL_VERSION);
        result["expected_addon_version"] = json!(env!("CARGO_PKG_VERSION"));
        result["up_to_date"] = json!(
            result["protocol_version"]
                .as_u64()
                .is_some_and(|v| v >= PROTOCOL_VERSION)
                && result["addon_build_version"] == env!("CARGO_PKG_VERSION")
        );
        result["update_command"] = json!("blender-mcp install-addon");
        result["after_install"] =
            json!("Restart Blender or disable and enable the add-on, then Start MCP Server");
    }
    Ok(CallToolResult::structured(result))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(name: &str, value: Value) -> Result<Map<String, Value>> {
        definitions()?
            .into_iter()
            .find(|d| d.tool.name == name)
            .unwrap()
            .arguments(value.as_object().unwrap().clone())
    }

    #[test]
    fn catalog_preserves_tools_without_collection_parameters() {
        let tools = definitions().unwrap();
        assert_eq!(tools.len(), 9);
        for definition in tools {
            assert!(!definition.tool.name.contains("telemetry"));
            assert!(!definition.tool.name.contains("trajectory"));
            assert!(
                !definition.tool.input_schema["properties"]
                    .as_object()
                    .unwrap()
                    .contains_key("user_prompt")
            );
        }
        assert!(args("execute_blender_code", json!({"code":4})).is_err());
        assert!(args("execute_blender_code", json!({})).is_err());
        assert!(
            args(
                "execute_blender_code",
                json!({"code":"pass", "unexpected":false})
            )
            .is_err()
        );
        assert!(args("get_viewport_screenshot", json!({"max_size":0})).is_err());
        for invalid in [
            json!({"offset":-1}),
            json!({"limit":0}),
            json!({"limit":101}),
            json!({"selected_only":"yes"}),
        ] {
            assert!(args("get_scene_info", invalid).is_err());
        }
        assert!(
            args(
                "execute_blender_code",
                json!({"code":"", "reset_namespace":true})
            )
            .is_err()
        );
        assert!(
            args(
                "execute_blender_code",
                json!({"code":"", "reset_namespace":false})
            )
            .is_ok()
        );
        assert!(
            args("get_scene_info", json!({"user_prompt":"ignored"}))
                .unwrap()
                .is_empty()
        );
    }

    #[tokio::test]
    async fn maps_tool_parameters_to_addon_commands() {
        assert_eq!(
            prepare(
                "get_object_info",
                args("get_object_info", json!({"object_name":"Cube"})).unwrap(),
            )
            .await
            .unwrap(),
            ("get_object_info".into(), json!({"name":"Cube"}))
        );
        let (_, params) = prepare(
            "download_sketchfab_model",
            args(
                "download_sketchfab_model",
                json!({"uid":"model", "target_size":1.7}),
            )
            .unwrap(),
        )
        .await
        .unwrap();
        assert_eq!(
            params,
            json!({"uid":"model","normalize_size":true,"target_size":1.7})
        );
    }
}
