use std::{
    sync::{Arc, Mutex},
    time::Duration,
};

use anyhow::Result;
use rmcp::{ErrorData, RoleServer, ServerHandler, model::*, service::RequestContext};
use serde_json::{Value, json};
use tokio::time::timeout;

use crate::{
    connection::BlenderConnection,
    tools::{self, ToolDefinition},
};

pub const INSTRUCTIONS: &str = include_str!("../resources/instructions.txt");

#[derive(Default)]
struct Sketchfab {
    enabled: Option<bool>,
    listed: Option<bool>,
}

pub struct BlenderServer {
    connection: Arc<BlenderConnection>,
    tools: Vec<ToolDefinition>,
    sketchfab: Mutex<Sketchfab>,
}

impl BlenderServer {
    pub fn new(host: String, port: u16) -> Result<Self> {
        Ok(Self {
            connection: Arc::new(BlenderConnection::new(host, port)),
            tools: tools::definitions()?,
            sketchfab: Mutex::default(),
        })
    }

    /// Records Blender's Sketchfab setting and reports whether the listed tools are now stale.
    fn observe_sketchfab(&self, addon_info: &Value) -> bool {
        let Some(enabled) = addon_info["runtime"]["sketchfab"]["enabled"].as_bool() else {
            return false;
        };
        let mut sketchfab = self.sketchfab.lock().unwrap();
        sketchfab.enabled = Some(enabled);
        sketchfab.listed.is_some_and(|listed| listed != enabled)
    }
}

impl ServerHandler for BlenderServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(
            ServerCapabilities::builder()
                .enable_tools()
                .enable_tool_list_changed()
                .enable_prompts()
                .build(),
        )
        .with_server_info(Implementation::new(
            "blender-mcp",
            env!("CARGO_PKG_VERSION"),
        ))
        .with_instructions(INSTRUCTIONS.trim_end())
    }

    async fn list_tools(
        &self,
        _: Option<PaginatedRequestParams>,
        _: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        if self.sketchfab.lock().unwrap().enabled.is_none()
            && let Ok(Ok(info)) = timeout(
                Duration::from_secs(2),
                self.connection.send("get_addon_info", json!({})),
            )
            .await
        {
            self.observe_sketchfab(&info);
        }
        // Without a known setting, list Sketchfab tools so they stay reachable.
        let show_sketchfab = {
            let mut sketchfab = self.sketchfab.lock().unwrap();
            let show = sketchfab.enabled != Some(false);
            sketchfab.listed = Some(show);
            show
        };
        Ok(ListToolsResult::with_all_items(
            self.tools
                .iter()
                .filter(|definition| show_sketchfab || !definition.tool.name.contains("sketchfab"))
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
        context: RequestContext<RoleServer>,
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
        if request.name == "get_addon_status"
            && let Ok(CallToolResult {
                structured_content: Some(info),
                ..
            }) = &result
            && self.observe_sketchfab(info)
        {
            let _ = context.peer.notify_tool_list_changed().await;
        }
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
