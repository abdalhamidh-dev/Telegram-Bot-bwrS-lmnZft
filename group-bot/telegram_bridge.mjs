import { createInterface } from "node:readline";
import { ReplitConnectors } from "@replit/connectors-sdk";

const connectors = new ReplitConnectors();
const input = createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
});

for await (const line of input) {
  if (!line.trim()) continue;

  try {
    const request = JSON.parse(line);
    const method = String(request.method ?? "").replace(/^\/+/, "");
    if (!method) throw new Error("Telegram method is required");

    const body = request.body;
    const response = await connectors.proxy("telegram", `/${method}`, {
      method: body === undefined ? "GET" : "POST",
      ...(body === undefined
        ? {}
        : {
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          }),
    });

    const payload = await response.json();
    process.stdout.write(
      `${JSON.stringify({ status: response.status, payload })}\n`,
    );
  } catch (error) {
    process.stdout.write(
      `${JSON.stringify({
        status: 500,
        payload: {
          ok: false,
          description: error instanceof Error ? error.message : String(error),
        },
      })}\n`,
    );
  }
}