import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const ALLOW_EDIT_SAFE_TOOLS = new Set([
	"read",
	"write",
	"edit",
	"grep",
	"find",
	"ls"
]);

export default function termchat(pi: ExtensionAPI) {
	let currentApproveMode = process.env.PI_TERMMATE_APPROVE_MODE || "allow-edit";

	pi.on("input", async (event) => {
		if (event.text.startsWith("/termchat-setting approve_mode=")) {
			currentApproveMode = event.text.replace("/termchat-setting approve_mode=", "").trim();
			return { action: "handled" };
		}
	});

	pi.on("tool_call", async (event, ctx) => {
		if (currentApproveMode === "accept-all") {
			return;
		}

		if (currentApproveMode === "allow-edit" && ALLOW_EDIT_SAFE_TOOLS.has(event.toolName)) {
			return;
		}

		// Create a specific string title so codeform chatprocessor can intercept it
		const title = `Tool Permission: ${event.toolName}`;
		
		// Encode the actual toolName and input so codeform can render them natively
		const message = JSON.stringify({
			toolName: event.toolName,
			input: event.input
		});
		
		// This will be sent over RPC as an extension_ui_request with method "confirm"
		const confirmed = await ctx.ui.confirm(title, message);
		
		if (!confirmed) {
			return {
				block: true,
				reason: "User denied permission via UI."
			};
		}
	});
}
