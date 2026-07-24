// verdict.ts — tiny CLI over the governance kernel, used by `./run.sh demo`.
// Prints one tab-separated line: "<verdict>\t<rationale>". This is the same
// classify() that the Pi guardrail runs; it just makes the decision visible,
// since print-mode pi shows a block as silence.
//
//   tsx src/verdict.ts "draft an email to a client, case #A12345"
import { classify } from "./classify.ts";

const text = process.argv.slice(2).join(" ");
const r = classify({ text });
process.stdout.write(`${r.verdict}\t${r.rationale}\n`);
