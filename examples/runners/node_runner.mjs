#!/usr/bin/env node
// Reference runner: upshift result protocol v1, in Node (JavaScript/TypeScript projects).
//
// Same contract as python_runner.py, in the language most of the un-adaptable rescue targets
// were written in. Three of the OpenAI-track cases say the same thing in their own words:
// `upshift adapt` is Python-only, so a TypeScript agent's five adapter files had to be
// hand-written, and "the hand-write took longer than any run"
// (rescue-ops ops/cases/ghi56-004/CASE.md:226-228, ops/cases/ghisdk-032/CASE.md:257-259).
// On the Anthropic track, `a-085` (LayerDynamics/plastiq, a pnpm monorepo) hand-wrote the
// adapter around a vitest suite and a `plastiq-gen` CLI that already ran the agent
// end to end. A native runner is 40 lines of glue over what those projects already have.
//
//   echo '{"protocol":1,"case_id":"demo","rep":1,"seed":7,"model":"stub-model-a",
//          "endpoint":"chat_completions","initial_state":{"tasks":[]},
//          "user_messages":["Add a task to call the dentist."],"patch_applied":false}' \
//     | node examples/runners/node_runner.mjs
//
// Rules (identical to the Python reference): ONE JSON object on stdout as the last line,
// everything else on stderr; a provider error is reported as `api_error`, NOT as a non-zero
// exit — exiting non-zero tells upshift its own harness failed, and a 400 from the provider is
// the most valuable observation a run can make.

const PROTOCOL = 1;

const SYSTEM_PROMPT =
  "You manage a task list. Use the tools; never claim a task exists unless a tool added it.";

const TOOLS = [
  {
    name: "add_task",
    description: "Add one task to the list.",
    parameters: { type: "object", properties: { title: { type: "string" } }, required: ["title"] },
  },
  { name: "list_tasks", description: "List every task.", parameters: { type: "object", properties: {} } },
];

const MODEL_BREAKS_ON_TOOLS = "stub-model-b";
const TOOLS_REJECTED =
  "Function tools with reasoning_effort are not supported on this endpoint; " +
  "use /v1/responses or set reasoning_effort to 'none'";

// The stand-in for the real SDK. In your project this is your provider client.
class StubProvider {
  constructor(model, endpoint) {
    this.model = model;
    this.endpoint = endpoint;
    this.turn = 0;
  }
  call(request) {
    const tools = request.tools || [];
    if (this.model === MODEL_BREAKS_ON_TOOLS && tools.length && this.endpoint === "chat_completions") {
      const error = new Error(TOOLS_REJECTED);
      error.status = 400;
      throw error;
    }
    this.turn += 1;
    const usage = { input_tokens: 100, output_tokens: 20 };
    const userText = (request.messages.find((m) => m.role === "user") || {}).content || "";
    if (this.turn === 1 && tools.length) {
      return {
        model: this.model,
        tool_calls: [{ id: "call_1", name: "add_task", arguments: { title: userText } }],
        text: "",
        usage,
      };
    }
    return { model: this.model, tool_calls: [], text: `Added: ${userText}`, usage };
  }
}

class Tools {
  constructor(initialState) {
    this.tasks = [...((initialState || {}).tasks || [])];
  }
  execute(name, args) {
    if (name === "add_task") {
      const title = String((args || {}).title || "");
      if (!title) return { error: "title is required" };
      this.tasks.push({ title, status: "open" });
      return { added: title, count: this.tasks.length };
    }
    if (name === "list_tasks") return { tasks: [...this.tasks] };
    return { error: `unknown tool ${name}` };
  }
  state() {
    return { tasks: [...this.tasks] };
  }
}

function runAgent(kase) {
  const provider = new StubProvider(kase.model, kase.endpoint);
  const tools = new Tools(kase.initial_state);
  const messages = [{ role: "system", content: SYSTEM_PROMPT }];
  const toolExecutions = [];
  const apiCalls = [];
  const usage = { input_tokens: 0, output_tokens: 0 };
  let finalMessage = "";

  const segments = kase.user_messages || [];
  for (let segment = 0; segment < segments.length; segment += 1) {
    messages.push({ role: "user", content: segments[segment] });
    for (let turn = 0; turn < 8; turn += 1) {
      const request = { model: kase.model, messages, tools: TOOLS };
      let response;
      try {
        response = provider.call(request);
      } catch (error) {
        apiCalls.push({
          request,
          response: null,
          error: { message: error.message, status_code: error.status || null },
        });
        return {
          final_message: "",
          tool_executions: toolExecutions,
          final_state: tools.state(),
          api_calls: apiCalls,
          api_error: {
            message: error.message,
            status_code: error.status || null,
            type: "invalid_request_error",
          },
          usage,
        };
      }
      apiCalls.push({ request, response, error: null });
      for (const [key, value] of Object.entries(response.usage || {})) {
        usage[key] = (usage[key] || 0) + value;
      }
      const calls = response.tool_calls || [];
      if (calls.length === 0) {
        finalMessage = String(response.text || "");
        messages.push({ role: "assistant", content: finalMessage });
        break;
      }
      messages.push({ role: "assistant", tool_calls: calls });
      for (const call of calls) {
        const result = tools.execute(call.name, call.arguments || {});
        toolExecutions.push({
          turn,
          segment,
          name: call.name,
          arguments: call.arguments || {},
          result,
        });
        messages.push({ role: "tool", content: JSON.stringify(result) });
      }
      process.stderr.write(`turn ${turn}: ${calls.length} tool call(s)\n`);
    }
  }

  return {
    final_message: finalMessage,
    tool_executions: toolExecutions,
    final_state: tools.state(),
    api_calls: apiCalls,
    api_error: null,
    usage,
  };
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

const raw = await readStdin();
let kase;
try {
  kase = JSON.parse(raw);
} catch (error) {
  process.stderr.write(`runner: stdin was not JSON (${error.message})\n`);
  process.exit(1);
}
if (kase.protocol !== PROTOCOL) {
  process.stderr.write(`runner: unsupported protocol ${JSON.stringify(kase.protocol)}\n`);
  process.exit(1);
}
const result = runAgent(kase);
result.protocol = PROTOCOL;
process.stdout.write(`${JSON.stringify(result)}\n`);
