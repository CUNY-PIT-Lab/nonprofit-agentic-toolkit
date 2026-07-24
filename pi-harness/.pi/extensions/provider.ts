// provider.ts — model wiring for the Non-Profit AI Toolkit's Pi agent.
//
// Registers GLM-5.2 on Ollama Cloud as an OpenAI-compatible provider, so the
// toolkit's open-weight model is reachable without the user editing
// ~/.pi/agent/models.json. The key stays in $OLLAMA_API_KEY (environment only,
// never written to disk) — mirroring the stdlib prototype's invariant. The
// bearer header is applied automatically for the "openai-completions" API type.
//
// Named "ollama-cloud" (not "ollama") so it does not clobber pi's built-in local
// Ollama provider. Run with:  pi --provider ollama-cloud --model glm-5.2
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  pi.registerProvider("ollama-cloud", {
    name: "Ollama Cloud",
    baseUrl: "https://ollama.com/v1",
    apiKey: "OLLAMA_API_KEY", // env var name; resolved at request time
    api: "openai-completions",
    models: [
      {
        id: "glm-5.2",
        name: "GLM 5.2",
        reasoning: false, // = the prototype's think:false speed setting
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 128000,
        maxTokens: 4096,
      },
    ],
  });
}
