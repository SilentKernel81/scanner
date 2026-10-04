// light-ocr reader for the scanner: card_ocr.py starts this once and keeps it running, so the
// models are loaded a single time. One JSON object per line on stdin and stdout:
//   {"id": 1, "image": "<base64 JPEG/PNG>"}  ->  {"id": 1, "lines": [{"text", "confidence", "box"}]}
// box is the text line's four corners [[x, y], ...] in image pixels, starting top-left.
import { createEngine } from "@arcships/light-ocr";
import { createInterface } from "node:readline";

const send = (message) => process.stdout.write(JSON.stringify(message) + "\n");
// The scanner went away: nothing left to answer
process.stdout.on("error", () => process.exit(0));

const engine = await createEngine({ execution: { provider: process.argv[2] || "auto" } });
send({ ready: true, provider: engine.info?.execution?.selectionTrace?.selectedProvider ?? null });

// Requests are answered in order (card_ocr.py sends one at a time)
for await (const line of createInterface({ input: process.stdin })) {
  if (!line.trim()) continue;
  let id = null;
  try {
    const request = JSON.parse(line);
    id = request.id;
    const result = await engine.recognizeEncoded(Buffer.from(request.image, "base64"));
    send({
      id,
      lines: result.lines.map((l) => ({
        text: l.text,
        confidence: l.confidence,
        box: l.box.map((p) => [p.x, p.y]),
      })),
    });
  } catch (error) {
    send({ id, error: String(error?.message ?? error) });
  }
}
await engine.close();
