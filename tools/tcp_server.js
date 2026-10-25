"use strict";

const net = require("node:net");

const HOST = process.env.TCP_HOST || "0.0.0.0";
const PORT = Number(process.env.TCP_PORT || 5000);
const WRAPPER_VERSION = 1;
const CLIENT_WPORT = 16;
const METER_WPORT = 1;
const MAX_WRAPPER_PAYLOAD = 4096;
const PROBE_INTERVAL_MS = 30000;

function timestamp() {
  return new Date().toISOString();
}

function buildWrapperFrame(sourceWport, destinationWport, payload) {
  if (payload.length > MAX_WRAPPER_PAYLOAD) {
    throw new Error(`DLMS Wrapper payload is too large: ${payload.length}`);
  }

  const frame = Buffer.alloc(8 + payload.length);
  frame.writeUInt16BE(WRAPPER_VERSION, 0);
  frame.writeUInt16BE(sourceWport, 2);
  frame.writeUInt16BE(destinationWport, 4);
  frame.writeUInt16BE(payload.length, 6);
  payload.copy(frame, 8);
  return frame;
}

const server = net.createServer((socket) => {
  const client = `${socket.remoteAddress}:${socket.remotePort}`;
  console.log(`[${timestamp()}] CONNECT ${client}`);

  socket.setKeepAlive(true, 30000);
  let receiveBuffer = Buffer.alloc(0);
  let sequence = 1;

  function sendWrapperProbe() {
    const payload = Buffer.from(`WRAPPER_PING,${sequence}`, "utf8");
    const frame = buildWrapperFrame(CLIENT_WPORT, METER_WPORT, payload);
    sequence += 1;

    socket.write(frame, () => {
      console.log(
        `[${timestamp()}] WRAPPER TX ${client} source=${CLIENT_WPORT} ` +
          `destination=${METER_WPORT} payload=${JSON.stringify(payload.toString("utf8"))}`,
      );
      console.log(`  hex : ${frame.toString("hex").toUpperCase()}`);
    });
  }

  sendWrapperProbe();
  const probeTimer = setInterval(sendWrapperProbe, PROBE_INTERVAL_MS);

  socket.on("data", (data) => {
    console.log(`[${timestamp()}] TCP RX ${client} ${data.length} bytes`);
    receiveBuffer = Buffer.concat([receiveBuffer, data]);

    while (receiveBuffer.length >= 8) {
      const version = receiveBuffer.readUInt16BE(0);
      const sourceWport = receiveBuffer.readUInt16BE(2);
      const destinationWport = receiveBuffer.readUInt16BE(4);
      const payloadLength = receiveBuffer.readUInt16BE(6);

      if (version !== WRAPPER_VERSION) {
        console.error(`[${timestamp()}] Invalid wrapper version ${version} from ${client}`);
        socket.destroy();
        return;
      }

      if (payloadLength > MAX_WRAPPER_PAYLOAD) {
        console.error(`[${timestamp()}] Oversized wrapper payload ${payloadLength} from ${client}`);
        socket.destroy();
        return;
      }

      const frameLength = 8 + payloadLength;
      if (receiveBuffer.length < frameLength) {
        return;
      }

      const frame = receiveBuffer.subarray(0, frameLength);
      const payload = frame.subarray(8);
      receiveBuffer = receiveBuffer.subarray(frameLength);

      console.log(
        `[${timestamp()}] WRAPPER RX ${client} version=${version} ` +
          `source=${sourceWport} destination=${destinationWport} length=${payloadLength}`,
      );
      console.log(`  payload: ${JSON.stringify(payload.toString("utf8"))}`);
      console.log(`  hex    : ${frame.toString("hex").toUpperCase()}`);
    }
  });

  socket.on("end", () => {
    console.log(`[${timestamp()}] END ${client}`);
  });

  socket.on("close", (hadError) => {
    clearInterval(probeTimer);
    console.log(`[${timestamp()}] CLOSE ${client} error=${hadError}`);
  });

  socket.on("error", (error) => {
    console.error(`[${timestamp()}] ERROR ${client}: ${error.message}`);
  });
});

server.on("error", (error) => {
  console.error(`[${timestamp()}] SERVER ERROR: ${error.message}`);
  process.exitCode = 1;
});

server.listen(PORT, HOST, () => {
  console.log(`[${timestamp()}] TCP server listening on ${HOST}:${PORT}`);
});

function shutdown(signal) {
  console.log(`[${timestamp()}] ${signal} received, shutting down`);
  server.close(() => process.exit(0));
}

process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));
