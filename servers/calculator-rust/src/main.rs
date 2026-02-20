use rmcp::{
    handler::server::tool::ToolRouter,
    model::*,
    schemars, tool, tool_handler, tool_router,
    transport::stdio,
    ErrorData as McpError, ServerHandler, ServiceExt,
};
use serde::Deserialize;

// --- Parameter types ---

#[derive(Debug, Deserialize, schemars::JsonSchema)]
struct TwoNumbers {
    #[schemars(description = "First number")]
    a: f64,
    #[schemars(description = "Second number")]
    b: f64,
}

// --- Calculator service ---

#[derive(Clone)]
pub struct Calculator {
    tool_router: ToolRouter<Self>,
}

#[tool_router]
impl Calculator {
    fn new() -> Self {
        Self {
            tool_router: Self::tool_router(),
        }
    }

    #[tool(description = "Add two numbers together")]
    async fn add(&self, params: Parameters<TwoNumbers>) -> Result<CallToolResult, McpError> {
        let TwoNumbers { a, b } = params.0;
        Ok(CallToolResult::success(vec![Content::text(
            (a + b).to_string(),
        )]))
    }

    #[tool(description = "Subtract the second number from the first")]
    async fn subtract(&self, params: Parameters<TwoNumbers>) -> Result<CallToolResult, McpError> {
        let TwoNumbers { a, b } = params.0;
        Ok(CallToolResult::success(vec![Content::text(
            (a - b).to_string(),
        )]))
    }

    #[tool(description = "Multiply two numbers together")]
    async fn multiply(&self, params: Parameters<TwoNumbers>) -> Result<CallToolResult, McpError> {
        let TwoNumbers { a, b } = params.0;
        Ok(CallToolResult::success(vec![Content::text(
            (a * b).to_string(),
        )]))
    }

    #[tool(description = "Divide the first number by the second")]
    async fn divide(&self, params: Parameters<TwoNumbers>) -> Result<CallToolResult, McpError> {
        let TwoNumbers { a, b } = params.0;
        if b == 0.0 {
            return Ok(CallToolResult::error(vec![Content::text(
                "Error: Division by zero",
            )]));
        }
        Ok(CallToolResult::success(vec![Content::text(
            (a / b).to_string(),
        )]))
    }
}

#[tool_handler]
impl ServerHandler for Calculator {
    fn get_info(&self) -> ServerInfo {
        ServerInfo {
            instructions: Some(
                "A simple arithmetic calculator. Supports add, subtract, multiply, and divide."
                    .into(),
            ),
            capabilities: ServerCapabilities::builder().enable_tools().build(),
            ..Default::default()
        }
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let service = Calculator::new()
        .serve(stdio())
        .await
        .inspect_err(|e| {
            eprintln!("Error starting server: {}", e);
        })?;
    service.waiting().await?;
    Ok(())
}
