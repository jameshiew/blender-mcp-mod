use std::sync::Arc;

use anyhow::Result;
use rmcp::{ErrorData, RoleServer, ServerHandler, model::*, service::RequestContext};

use crate::{
    connection::BlenderConnection,
    tools::{self, ToolDefinition},
};

pub struct BlenderServer {
    connection: Arc<BlenderConnection>,
    tools: Vec<ToolDefinition>,
}

impl BlenderServer {
    pub fn new(host: String, port: u16) -> Result<Self> {
        Ok(Self {
            connection: Arc::new(BlenderConnection::new(host, port)),
            tools: tools::definitions()?,
        })
    }
}

impl ServerHandler for BlenderServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(ServerCapabilities::builder().enable_tools().enable_prompts().build())
            .with_server_info(Implementation::new("blender-mcp", env!("CARGO_PKG_VERSION")))
            .with_instructions("Control Blender through its MCP extension. Start with get_addon_status and get_scene_info; follow next_offset when more objects are needed. Inspect exact object names before editing. Use get_blender_api_info to inspect installed RNA type properties, functions, and operator parameters before writing Python; static enums may omit context-dependent choices. Rotation arrays follow rotation_mode. File loads and undo/redo clear execution namespaces to discard invalid Blender references. Use get_object_info(details=true) for concise material, modifier, and animation summaries; use get_material_info, get_node_group_info, get_modifier_info, and get_animation_info for focused inspection. Follow has_more/next_offset and check details_omitted/omitted fields before assuming inspection is complete. Use set_camera for lens, sensor, depth of field, panoramic projection, and camera framing; use set_viewport for view direction, framing, zoom, and shading. Use execute_blender_code for modeling, materials, and scene operations, and Sketchfab when external assets suit the task. Batch related edits, print concise results, and verify visible changes with get_viewport_screenshot. For stills, use start_render; for PNG sequences, use start_animation_render with a new output directory. Poll get_render_status, retrieve get_render_image or save export_render, and use cancel_render to stop a job. Animation status includes completed and total frame counts; previews and exports accept a completed frame while rendering. Use resume_animation_render with the output directory to continue after cancellation or server restart from the original snapshot. For recoverable edits, opt into checkpoint and summarize_changes on execute_blender_code. Errors and timeouts can leave partial changes: check started, succeeded, and partial_changes; use get_execution_result for opted-in calls and get_execution_changes for additional change pages before retrying. restore_checkpoint saves a safety checkpoint, opens a working copy, and clears all namespaces. Credit assets using returned attribution. JSON results are available as structuredContent and text; image tools also return capture metadata.")
    }

    async fn list_tools(
        &self,
        _: Option<PaginatedRequestParams>,
        _: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        Ok(ListToolsResult::with_all_items(
            self.tools
                .iter()
                .map(|definition| definition.tool.clone())
                .collect(),
        ))
    }

    fn get_tool(&self, name: &str) -> Option<Tool> {
        self.tools
            .iter()
            .find(|definition| definition.tool.name == name)
            .map(|definition| definition.tool.clone())
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        _: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        let definition = self
            .tools
            .iter()
            .find(|definition| definition.tool.name == request.name)
            .ok_or_else(|| {
                ErrorData::new(
                    ErrorCode::METHOD_NOT_FOUND,
                    format!("Unknown tool: {}", request.name),
                    None,
                )
            })?;
        let result = match definition.arguments(request.arguments.unwrap_or_default()) {
            Ok(args) => tools::execute(&self.connection, &request.name, args).await,
            Err(error) => Err(error),
        };
        Ok(match result {
            Ok(result) => result,
            Err(error) => CallToolResult::error(vec![ContentBlock::text(format!("{error:#}"))]),
        }
        .into())
    }

    async fn list_prompts(
        &self,
        _: Option<PaginatedRequestParams>,
        _: RequestContext<RoleServer>,
    ) -> Result<ListPromptsResult, ErrorData> {
        Ok(ListPromptsResult::with_all_items(vec![Prompt::new(
            "asset_creation_strategy",
            Some("Strategy for sourcing, generating, and verifying Blender assets"),
            None,
        )]))
    }

    async fn get_prompt(
        &self,
        request: GetPromptRequestParams,
        _: RequestContext<RoleServer>,
    ) -> Result<GetPromptResponse, ErrorData> {
        if request.name != "asset_creation_strategy" {
            return Err(ErrorData::invalid_params("Unknown prompt", None));
        }
        Ok(GetPromptResult::new(vec![PromptMessage::new_text(
            Role::User,
            include_str!("../resources/asset_creation_strategy.txt"),
        )])
        .into())
    }
}
