import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { resolve } from "node:path";
import { Router, type Request, type Response } from "express";
import { readSession } from "./auth";

const router = Router();
const execFileAsync = promisify(execFile);
const GUILD_ID = 1550859193890635847;
type BridgePayload = {
  error?: unknown;
  guild_id?: unknown;
  scrims?: unknown;
  [key: string]: unknown;
};

function remoteBridgeUrl(path = "") {
  const base = process.env.BETA_WEB_BRIDGE_URL?.trim().replace(/\/+$/, "");
  return base ? `${base}${path}` : undefined;
}

function remoteBridgeHeaders() {
  const secret = process.env.BETA_WEB_BRIDGE_SECRET?.trim();
  if (!secret) throw new Error("BETA_WEB_BRIDGE_SECRET is not configured");
  return {
    Authorization: `Bearer ${secret}`,
    "Content-Type": "application/json",
  };
}

async function callRemoteBridge(
  path: string,
  init: RequestInit = {},
) : Promise<BridgePayload> {
  const response = await fetch(remoteBridgeUrl(path)!, {
    ...init,
    headers: {
      ...remoteBridgeHeaders(),
      ...(init.headers ?? {}),
    },
    signal: AbortSignal.timeout(8000),
  });
  const payload = (await response.json().catch(() => ({}))) as BridgePayload;
  if (!response.ok) {
    throw new Error(
      typeof payload?.error === "string"
        ? payload.error
        : `Remote beta bridge returned ${response.status}`,
    );
  }
  return payload;
}

function bridgePath() {
  return process.env.BETA_WEB_BRIDGE_PATH
    ? resolve(process.env.BETA_WEB_BRIDGE_PATH)
    : resolve(process.cwd(), "../../web_beta_bridge.py");
}

function statePath() {
  return process.env.BETA_STATE_PATH
    ? resolve(process.env.BETA_STATE_PATH)
    : resolve(process.cwd(), "../../data/slots.sqlite3");
}

router.get("/scrims", async (req: Request, res: Response) => {
  if (!readSession(req)) {
    res.status(401).json({ error: "Authentication required." });
    return;
  }

  try {
    if (remoteBridgeUrl()) {
      const payload = await callRemoteBridge(`/v1/setup/${GUILD_ID}`);
      if (payload.guild_id !== GUILD_ID || !Array.isArray(payload.scrims)) {
        throw new Error("The remote beta bridge returned an invalid snapshot.");
      }
      res.json(payload);
      return;
    }
    const result = await execFileAsync(
      "python",
      [bridgePath(), String(GUILD_ID), statePath()],
      { timeout: 5000, maxBuffer: 8 * 1024 * 1024 },
    );
    const payload = JSON.parse(result.stdout);
    if (payload.guild_id !== GUILD_ID || !Array.isArray(payload.scrims)) {
      throw new Error("The beta bridge returned an invalid guild snapshot.");
    }
    res.json(payload);
  } catch (error) {
    console.error(
      "[scrims] Could not read the beta snapshot:",
      error instanceof Error ? error.message : "Unknown error",
    );
    res.status(503).json({
      error: "The beta scrim snapshot is temporarily unavailable.",
    });
  }
});

router.post("/scrims", async (req: Request, res: Response) => {
  if (!readSession(req)) {
    res.status(401).json({ error: "Authentication required." });
    return;
  }
  if (!remoteBridgeUrl()) {
    res.status(503).json({ error: "The remote beta bridge is not configured." });
    return;
  }
  try {
    res.status(201).json(
      await callRemoteBridge(`/v1/setup/${GUILD_ID}/scrims`, {
        method: "POST",
        body: JSON.stringify(req.body),
      }),
    );
  } catch (error) {
    console.error("[scrims] Remote create failed:", error);
    res.status(503).json({ error: "The beta bot bridge is unavailable." });
  }
});

router.patch("/scrims/:scrimId", async (req: Request, res: Response) => {
  if (!readSession(req)) {
    res.status(401).json({ error: "Authentication required." });
    return;
  }
  if (!remoteBridgeUrl()) {
    res.status(503).json({ error: "The remote beta bridge is not configured." });
    return;
  }
  try {
    const scrimId = String(req.params.scrimId);
    res.json(
      await callRemoteBridge(
        `/v1/setup/${GUILD_ID}/scrims/${encodeURIComponent(scrimId)}`,
        { method: "PATCH", body: JSON.stringify(req.body) },
      ),
    );
  } catch (error) {
    console.error("[scrims] Remote update failed:", error);
    res.status(503).json({ error: "The beta bot bridge is unavailable." });
  }
});

router.post("/scrims/:scrimId/actions", async (req: Request, res: Response) => {
  if (!readSession(req)) {
    res.status(401).json({ error: "Authentication required." });
    return;
  }
  if (!remoteBridgeUrl()) {
    res.status(503).json({ error: "The remote beta bridge is not configured." });
    return;
  }
  try {
    const scrimId = String(req.params.scrimId);
    res.json(
      await callRemoteBridge(
        `/v1/setup/${GUILD_ID}/scrims/${encodeURIComponent(scrimId)}/actions`,
        { method: "POST", body: JSON.stringify(req.body) },
      ),
    );
  } catch (error) {
    console.error("[scrims] Remote action failed:", error);
    res.status(503).json({ error: "The beta bot bridge is unavailable." });
  }
});

router.delete("/scrims/:scrimId", async (req: Request, res: Response) => {
  if (!readSession(req)) {
    res.status(401).json({ error: "Authentication required." });
    return;
  }
  if (!remoteBridgeUrl()) {
    res.status(503).json({ error: "The remote beta bridge is not configured." });
    return;
  }
  try {
    const scrimId = String(req.params.scrimId);
    res.json(
      await callRemoteBridge(
        `/v1/setup/${GUILD_ID}/scrims/${encodeURIComponent(scrimId)}`,
        { method: "DELETE" },
      ),
    );
  } catch (error) {
    console.error("[scrims] Remote delete failed:", error);
    res.status(503).json({ error: "The beta bot bridge is unavailable." });
  }
});

export default router;