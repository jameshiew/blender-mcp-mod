use std::{collections::HashMap, path::Path, sync::Arc};

use anyhow::{Context, Result, bail, ensure};
use base64::{Engine, prelude::BASE64_STANDARD};
use rmcp::model::{CallToolResult, ContentBlock, Tool};
use serde_json::{Map, Value, json};
use tokio::io::AsyncReadExt;

use crate::{
    addon::PROTOCOL_VERSION,
    connection::{BlenderConnection, MAX_MESSAGE_BYTES},
};

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

pub fn safe_mode_enabled() -> bool {
    std::env::var("BLENDER_MCP_SAFE_MODE").is_ok_and(|v| {
        matches!(
            v.trim().to_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        )
    })
}

pub fn guarded_code(code: &str) -> String {
    let validator = serde_json::to_string(include_str!("../resources/safe_mode.py"))
        .expect("string serialization");
    let code = serde_json::to_string(code).expect("string serialization");
    format!(
        "_guard = {{}}\nexec({validator}, _guard)\n_guard['validate_code']({code})\nexec({code}, {{'bpy': bpy}})\n"
    )
}

pub async fn prepare(
    name: &str,
    mut args: Map<String, Value>,
    safe_mode: bool,
) -> Result<(String, Value)> {
    let command = match name {
        "get_addon_status" => "get_addon_info",
        "get_object_info" => {
            let value = args.remove("object_name").context("Missing object_name")?;
            args.insert("name".into(), value);
            name
        }
        "execute_blender_code" => {
            if safe_mode {
                let code = args["code"].as_str().context("Missing code")?;
                args.insert("code".into(), json!(guarded_code(code)));
            }
            "execute_code"
        }
        "download_sketchfab_model" => {
            args.insert("normalize_size".into(), json!(true));
            name
        }
        "search_polypizza_models" => {
            args.insert("category".into(), polypizza_id(&args["category"], false)?);
            args.insert("licence".into(), polypizza_id(&args["licence"], true)?);
            ensure!(
                !args["query"].as_str().unwrap_or_default().trim().is_empty()
                    || !args["category"].is_null()
                    || !args["licence"].is_null()
                    || args["animated"] == true,
                "Poly Pizza needs a keyword or at least one category, licence, or animated filter"
            );
            name
        }
        "generate_hyper3d_model_via_text" | "generate_hyper3d_model_via_images" => {
            let bbox = process_bbox(&args["bbox_condition"])?;
            let (prompt, images) = if name.ends_with("_text") {
                (
                    args.remove("text_prompt").context("Missing text_prompt")?,
                    Value::Null,
                )
            } else {
                (Value::Null, rodin_images(&args).await?)
            };
            args = json!({"text_prompt":prompt,"images":images,"bbox_condition":bbox})
                .as_object()
                .unwrap()
                .clone();
            "create_rodin_job"
        }
        "poll_rodin_job_status" => {
            exactly_one(&args, "subscription_key", "request_id")?;
            args.retain(|_, value| !value.is_null());
            name
        }
        "import_generated_asset" => {
            exactly_one(&args, "task_uuid", "request_id")?;
            args.retain(|_, value| !value.is_null());
            name
        }
        "generate_hunyuan3d_model" => {
            ensure!(
                nonempty(&args["text_prompt"]) || nonempty(&args["input_image_url"]),
                "Provide text_prompt or input_image_url"
            );
            let image = args.remove("input_image_url").unwrap_or(Value::Null);
            args.insert("image".into(), image);
            "create_hunyuan_job"
        }
        "poll_hunyuan_job_status" => {
            ensure!(nonempty(&args["job_id"]), "Provide job_id");
            name
        }
        _ => name,
    };
    Ok((command.into(), Value::Object(args)))
}

fn nonempty(value: &Value) -> bool {
    value.as_str().is_some_and(|s| !s.trim().is_empty())
}

fn exactly_one(args: &Map<String, Value>, first: &str, second: &str) -> Result<()> {
    ensure!(
        nonempty(&args[first]) ^ nonempty(&args[second]),
        "Provide exactly one of {first} and {second}"
    );
    Ok(())
}

async fn rodin_images(args: &Map<String, Value>) -> Result<Value> {
    let paths = args["input_image_paths"].as_array();
    let urls = args["input_image_urls"].as_array();
    ensure!(
        paths.is_some() ^ urls.is_some(),
        "Provide exactly one of input_image_paths and input_image_urls"
    );
    let mut images = Vec::new();
    let mut encoded_bytes = 0;
    if let Some(paths) = paths {
        for path in paths {
            let path = Path::new(path.as_str().context("Image path must be a string")?);
            ensure!(path.is_absolute(), "Image paths must be absolute");
            let file = tokio::fs::File::open(path)
                .await
                .with_context(|| format!("Cannot open image {}", path.display()))?;
            ensure!(
                file.metadata().await?.is_file(),
                "Image path must be a regular file"
            );
            let mut data = Vec::new();
            file.take(16 * 1024 * 1024 + 1)
                .read_to_end(&mut data)
                .await?;
            ensure!(
                !data.is_empty() && data.len() <= 16 * 1024 * 1024,
                "Images must contain between 1 byte and 16 MiB"
            );
            let extension = path
                .extension()
                .and_then(|s| s.to_str())
                .context("Image path needs a file extension")?;
            encoded_bytes += data.len().div_ceil(3) * 4 + extension.len() + 10;
            ensure!(
                encoded_bytes <= MAX_MESSAGE_BYTES,
                "Combined images exceed the 32 MiB command limit"
            );
            images.push(json!([
                format!(".{extension}"),
                BASE64_STANDARD.encode(data)
            ]));
        }
    }
    if let Some(urls) = urls {
        for value in urls {
            let url = url::Url::parse(value.as_str().context("Image URL must be a string")?)
                .context("Invalid image URL")?;
            ensure!(
                matches!(url.scheme(), "https" | "http") && url.host_str().is_some(),
                "Image URLs must use HTTP or HTTPS"
            );
            images.push(value.clone());
        }
    }
    ensure!(!images.is_empty(), "Provide at least one image");
    Ok(Value::Array(images))
}

fn process_bbox(value: &Value) -> Result<Value> {
    if value.is_null() {
        return Ok(Value::Null);
    }
    let values = value
        .as_array()
        .context("bbox_condition must be an array")?;
    ensure!(values.len() == 3, "bbox_condition needs three dimensions");
    let numbers = values
        .iter()
        .map(|v| {
            v.as_f64()
                .filter(|n| n.is_finite() && *n > 0.0)
                .context("bbox_condition dimensions must be positive")
        })
        .collect::<Result<Vec<_>>>()?;
    if values.iter().all(Value::is_u64) {
        return Ok(value.clone());
    }
    let maximum = numbers.iter().copied().fold(0.0, f64::max);
    Ok(json!(
        numbers
            .iter()
            .map(|n| ((n / maximum * 100.0) as u64).max(1))
            .collect::<Vec<_>>()
    ))
}

fn polypizza_id(value: &Value, licence: bool) -> Result<Value> {
    if value.is_null() || value == "" {
        return Ok(Value::Null);
    }
    let maximum = if licence { 1 } else { 11 };
    if let Some(number) = value
        .as_i64()
        .or_else(|| value.as_str()?.trim().parse().ok())
    {
        ensure!(
            (0..=maximum).contains(&number),
            "Poly Pizza filter ID must be between 0 and {maximum}"
        );
        return Ok(json!(number));
    }
    let text = value
        .as_str()
        .context("Poly Pizza filter must be a name or integer ID")?;
    let normalized: String = text
        .to_lowercase()
        .chars()
        .filter(|c| c.is_alphanumeric())
        .collect();
    if licence {
        if normalized.starts_with("ccby") {
            return Ok(json!(0));
        }
        if normalized.starts_with("cc0") || normalized == "publicdomain" {
            return Ok(json!(1));
        }
    } else {
        let categories: HashMap<String, u8> =
            serde_json::from_str(include_str!("../resources/polypizza_categories.json"))?;
        if let Some(id) = categories.get(&normalized) {
            return Ok(json!(id));
        }
    }
    bail!(
        "Unknown Poly Pizza {}: {text}",
        if licence { "licence" } else { "category" }
    );
}

pub async fn execute(
    connection: &Arc<BlenderConnection>,
    name: &str,
    args: Map<String, Value>,
    safe_mode: bool,
) -> Result<CallToolResult> {
    let (command, params) = prepare(name, args, safe_mode).await?;
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
        return Ok(CallToolResult::success(vec![ContentBlock::image(
            data, mime,
        )]));
    }
    if name == "get_addon_status" {
        ensure!(result.is_object(), "Invalid add-on information");
        result["expected_protocol_version"] = json!(PROTOCOL_VERSION);
        result["up_to_date"] = json!(
            result["protocol_version"]
                .as_u64()
                .is_some_and(|v| v >= PROTOCOL_VERSION)
        );
        result["update_command"] = json!("blender-mcp install-addon");
        result["after_install"] =
            json!("Restart Blender or disable and enable the add-on, then Start MCP Server");
    }
    if command == "create_rodin_job" && result.get("submit_time").is_some() {
        result = json!({"task_uuid":result["uuid"], "subscription_key":result["jobs"]["subscription_key"]});
    }
    if command == "create_hunyuan_job"
        && let Some(id) = result["Response"]["JobId"].as_str()
    {
        result = json!({"job_id":format!("job_{id}")});
    }
    Ok(CallToolResult::success(vec![ContentBlock::text(
        serde_json::to_string_pretty(&result)?,
    )]))
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
        assert_eq!(tools.len(), 26);
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
                json!({"code":"pass", "safe_mode":false})
            )
            .is_err()
        );
        assert!(args("get_viewport_screenshot", json!({"max_size":0})).is_err());
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
                false
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
            false,
        )
        .await
        .unwrap();
        assert_eq!(
            params,
            json!({"uid":"model","normalize_size":true,"target_size":1.7})
        );
        let (_, params) = prepare(
            "search_polypizza_models",
            args(
                "search_polypizza_models",
                json!({"category":"Animals", "licence":"CC0"}),
            )
            .unwrap(),
            false,
        )
        .await
        .unwrap();
        assert_eq!(params["category"], 7);
        assert_eq!(params["licence"], 1);
        assert!(
            prepare(
                "search_polypizza_models",
                args("search_polypizza_models", json!({})).unwrap(),
                false
            )
            .await
            .is_err()
        );
    }

    #[test]
    fn filter_names_aliases_and_invalid_ids() {
        for (name, expected) in [
            ("furniture & decor", 4),
            ("buildings/architecture", 8),
            ("person", 9),
            ("plants", 6),
            ("3", 3),
        ] {
            assert_eq!(polypizza_id(&json!(name), false).unwrap(), expected);
        }
        assert_eq!(polypizza_id(&json!("CC-BY 3.0"), true).unwrap(), 0);
        assert_eq!(polypizza_id(&json!("Public Domain"), true).unwrap(), 1);
        for value in [json!(true), json!(-1), json!(12), json!("spaceships")] {
            assert!(polypizza_id(&value, false).is_err());
        }
        for value in [json!(true), json!(2), json!("GPL")] {
            assert!(polypizza_id(&value, true).is_err());
        }
    }

    #[test]
    fn bounding_boxes_remain_positive() {
        assert_eq!(process_bbox(&json!([1, 2, 3])).unwrap(), json!([1, 2, 3]));
        assert_eq!(
            process_bbox(&json!([0.001, 1.0, 2.0])).unwrap(),
            json!([1, 50, 100])
        );
        for value in [
            json!([0, 1, 1]),
            json!([-1, 1, 1]),
            json!([]),
            json!([1, 2]),
        ] {
            assert!(process_bbox(&value).is_err());
        }
    }

    #[tokio::test]
    async fn rodin_url_inputs_work_and_conflicts_fail() {
        let name = "generate_hyper3d_model_via_images";
        let (_, params) = prepare(
            name,
            args(
                name,
                json!({"input_image_urls":["https://example.com/image.png"]}),
            )
            .unwrap(),
            false,
        )
        .await
        .unwrap();
        assert_eq!(
            params,
            json!({"text_prompt":null,"images":["https://example.com/image.png"],"bbox_condition":null})
        );
        for value in [
            json!({}),
            json!({"input_image_urls":[]}),
            json!({"input_image_urls":["file:///tmp/a"]}),
            json!({"input_image_paths":[],"input_image_urls":[]}),
        ] {
            assert!(
                prepare(name, args(name, value).unwrap(), false)
                    .await
                    .is_err()
            );
        }
    }
}
