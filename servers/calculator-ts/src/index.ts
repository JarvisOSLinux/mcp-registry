#!/usr/bin/env node

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const server = new McpServer({
  name: "calculator-ts",
  version: "1.0.0",
});

// --- Tools ---

server.tool(
  "add",
  "Add two numbers together",
  {
    a: z.number().describe("First number"),
    b: z.number().describe("Second number"),
  },
  async ({ a, b }) => ({
    content: [{ type: "text", text: String(a + b) }],
  })
);

server.tool(
  "subtract",
  "Subtract the second number from the first",
  {
    a: z.number().describe("First number"),
    b: z.number().describe("Second number"),
  },
  async ({ a, b }) => ({
    content: [{ type: "text", text: String(a - b) }],
  })
);

server.tool(
  "multiply",
  "Multiply two numbers together",
  {
    a: z.number().describe("First number"),
    b: z.number().describe("Second number"),
  },
  async ({ a, b }) => ({
    content: [{ type: "text", text: String(a * b) }],
  })
);

server.tool(
  "divide",
  "Divide the first number by the second",
  {
    a: z.number().describe("Dividend"),
    b: z.number().describe("Divisor (must not be zero)"),
  },
  async ({ a, b }) => {
    if (b === 0) {
      return {
        isError: true,
        content: [{ type: "text", text: "Error: Division by zero" }],
      };
    }
    return {
      content: [{ type: "text", text: String(a / b) }],
    };
  }
);

// --- Start ---

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("Calculator MCP Server (TypeScript) running on stdio");
}

main().catch((error) => {
  console.error("Fatal error in main():", error);
  process.exit(1);
});
