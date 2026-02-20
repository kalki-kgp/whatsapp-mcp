import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  WASocket,
  makeCacheableSignalKeyStore,
  fetchLatestWaWebVersion,
} from "@whiskeysockets/baileys";
import express, { Request, Response } from "express";
import pino from "pino";
import * as QRCode from "qrcode";
import { Boom } from "@hapi/boom";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const AUTH_DIR = join(__dirname, "..", "auth_info");
const PORT = parseInt(process.env.BRIDGE_PORT || "3010", 10);

const logger = pino({ level: process.env.LOG_LEVEL || "warn" });

// --- State ---
let sock: WASocket | null = null;
let qrDataURL: string | null = null;
let connectionStatus: "disconnected" | "qr_pending" | "connected" = "disconnected";
let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 10;

// --- Baileys connection ---
async function connectToWhatsApp(): Promise<void> {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  // Fetch the latest WhatsApp web version to avoid 405 rejections
  const { version, isLatest } = await fetchLatestWaWebVersion({});
  console.log(`[bridge] Using WA web version ${version.join(".")}, isLatest: ${isLatest}`);

  sock = makeWASocket({
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, logger),
    },
    version,
    logger,
    browser: ["WhatsApp Assistant", "Chrome", "130.0.0"],
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      // Generate QR for HTTP API
      try {
        qrDataURL = await QRCode.toDataURL(qr, { width: 300, margin: 2 });
      } catch {
        qrDataURL = null;
      }
      // Print QR to terminal
      try {
        const terminalQR = await QRCode.toString(qr, { type: "terminal", small: true });
        console.log("\n" + terminalQR);
      } catch {
        // fall through
      }
      connectionStatus = "qr_pending";
      console.log("[bridge] QR code generated — scan with your phone");
    }

    if (connection === "open") {
      connectionStatus = "connected";
      qrDataURL = null;
      reconnectAttempts = 0;
      console.log("[bridge] Connected to WhatsApp");
    }

    if (connection === "close") {
      connectionStatus = "disconnected";
      qrDataURL = null;

      const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

      if (shouldReconnect && reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
        reconnectAttempts++;
        const delay = Math.min(1000 * 2 ** reconnectAttempts, 60000);
        console.log(
          `[bridge] Disconnected (code ${statusCode}). Reconnecting in ${delay / 1000}s (attempt ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})...`
        );
        setTimeout(connectToWhatsApp, delay);
      } else if (statusCode === DisconnectReason.loggedOut) {
        console.log("[bridge] Logged out. Delete auth_info/ and restart to re-authenticate.");
      } else {
        console.log("[bridge] Max reconnect attempts reached. Restart the bridge manually.");
      }
    }
  });
}

// --- Express API ---
const app = express();
app.use(express.json());

app.get("/api/status", (_req: Request, res: Response) => {
  res.json({ status: connectionStatus });
});

app.get("/api/qr", (_req: Request, res: Response) => {
  if (connectionStatus === "qr_pending" && qrDataURL) {
    res.json({ qr: qrDataURL });
  } else if (connectionStatus === "connected") {
    res.json({ message: "Already connected", status: "connected" });
  } else {
    res.json({ message: "No QR code available", status: connectionStatus });
  }
});

app.post("/api/send", async (req: Request, res: Response) => {
  const { recipient, message } = req.body;

  if (!recipient || !message) {
    res.status(400).json({ error: "Missing 'recipient' or 'message' in request body" });
    return;
  }

  if (connectionStatus !== "connected" || !sock) {
    res.status(503).json({ error: "WhatsApp bridge is not connected", status: connectionStatus });
    return;
  }

  // Normalize recipient JID
  let jid = recipient;
  if (!jid.includes("@")) {
    // Assume it's a phone number, add @s.whatsapp.net
    jid = jid.replace(/[^0-9]/g, "") + "@s.whatsapp.net";
  }

  try {
    const sent = await sock.sendMessage(jid, { text: message });
    console.log(`[bridge] Message sent to ${jid}`);
    res.json({
      success: true,
      recipient: jid,
      message_id: sent?.key?.id || null,
    });
  } catch (err: unknown) {
    const errorMsg = err instanceof Error ? err.message : String(err);
    console.error(`[bridge] Failed to send message to ${jid}:`, errorMsg);
    res.status(500).json({ error: `Failed to send message: ${errorMsg}` });
  }
});

// --- Start ---
app.listen(PORT, () => {
  console.log(`[bridge] WhatsApp bridge listening on http://localhost:${PORT}`);
  connectToWhatsApp();
});
