#!/usr/bin/env node
// Asserts every documented way to start the UI listens on 127.0.0.1 by
// default, and only rebinds when PORTUNUS_UI_HOST is set explicitly.
// Run after `npm run build` (the standalone case needs .next/standalone):
//
//   npm run build && npm run test:bind
//
// Each case spawns the real command, waits for the port to listen, reads the
// kernel's listening address for that port, then kills the process group.
// The ambient HOSTNAME is set to 0.0.0.0 on purpose: Docker and some shells
// export HOSTNAME, and Next's standalone server.js binds whatever it says.
import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const UI_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const CASES = [
  { name: "npm start", cmd: "npm start -- --port $PORT", env: {}, expect: "127.0.0.1" },
  { name: "npm run dev", cmd: "npm run dev -- --port $PORT", env: {}, expect: "127.0.0.1" },
  // The README's supervised-service command, verbatim apart from the port.
  { name: "README standalone", cmd: "npm run start:standalone", env: {}, expect: "127.0.0.1" },
  { name: "npm start + override", cmd: "npm start -- --port $PORT", env: { PORTUNUS_UI_HOST: "0.0.0.0" }, expect: "0.0.0.0" },
  { name: "README standalone + override", cmd: "npm run start:standalone", env: { PORTUNUS_UI_HOST: "0.0.0.0" }, expect: "0.0.0.0" },
];

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
    srv.on("error", reject);
  });
}

// Linux: the listening socket's local address straight from /proc/net/tcp{,6}.
function procListenAddrs(port) {
  const addrs = [];
  for (const [file, v6] of [["/proc/net/tcp", false], ["/proc/net/tcp6", true]]) {
    let text;
    try {
      text = fs.readFileSync(file, "utf8");
    } catch {
      continue;
    }
    for (const line of text.trim().split("\n").slice(1)) {
      const [, local, , state] = line.trim().split(/\s+/);
      const [hexAddr, hexPort] = local.split(":");
      if (state !== "0A" || parseInt(hexPort, 16) !== port) continue; // 0A = LISTEN
      addrs.push(v6 ? decodeV6(hexAddr) : decodeV4(hexAddr));
    }
  }
  return addrs;
}

function decodeV4(hex) {
  // Little-endian u32.
  return [3, 2, 1, 0].map((i) => parseInt(hex.slice(i * 2, i * 2 + 2), 16)).join(".");
}

function decodeV6(hex) {
  if (/^0{32}$/.test(hex)) return "::";
  if (hex === "00000000000000000000000001000000") return "::1";
  if (hex.startsWith("0000000000000000FFFF0000")) return decodeV4(hex.slice(24));
  return hex;
}

// Elsewhere (macOS): connect to every non-loopback IPv4 address; a loopback
// bind refuses them all, a wildcard bind accepts.
async function probeListenAddrs(port) {
  const external = Object.values(os.networkInterfaces())
    .flat()
    .filter((i) => i && i.family === "IPv4" && !i.internal)
    .map((i) => i.address);
  const reachable = [];
  for (const addr of external) {
    if (await canConnect(addr, port)) reachable.push(addr);
  }
  if (reachable.length) return ["0.0.0.0"];
  return (await canConnect("127.0.0.1", port)) ? ["127.0.0.1"] : [];
}

function canConnect(host, port) {
  return new Promise((resolve) => {
    const sock = net.connect({ host, port, timeout: 1000 });
    sock.on("connect", () => {
      sock.destroy();
      resolve(true);
    });
    sock.on("error", () => resolve(false));
    sock.on("timeout", () => {
      sock.destroy();
      resolve(false);
    });
  });
}

async function listenAddrs(port) {
  return fs.existsSync("/proc/net/tcp") ? procListenAddrs(port) : probeListenAddrs(port);
}

async function runCase(c) {
  const port = await freePort();
  const child = spawn("sh", ["-c", c.cmd], {
    cwd: UI_DIR,
    env: { ...process.env, HOSTNAME: "0.0.0.0", PORTUNUS_UI_HOST: "", ...c.env, PORT: String(port) },
    stdio: ["ignore", "ignore", "pipe"],
    detached: true,
  });
  let stderr = "";
  child.stderr.on("data", (d) => (stderr += d));
  try {
    const deadline = Date.now() + 90_000;
    let addrs = [];
    while (Date.now() < deadline && child.exitCode === null) {
      addrs = await listenAddrs(port);
      if (addrs.length) break;
      await new Promise((r) => setTimeout(r, 500));
    }
    if (!addrs.length) {
      return { ok: false, msg: `${c.name}: never listened on ${port}\n${stderr.slice(-2000)}` };
    }
    const ok = addrs.every((a) => a === c.expect);
    return { ok, msg: `${c.name}: listening on ${addrs.join(", ")} (expected ${c.expect})` };
  } finally {
    try {
      process.kill(-child.pid, "SIGTERM");
    } catch {}
  }
}

// The npm scripts read PORTUNUS_UI_HOST with an unset-or-empty default, so the
// empty string passed above for the default cases behaves as "unset".
let failed = 0;
for (const c of CASES) {
  const { ok, msg } = await runCase(c);
  console.log(`${ok ? "PASS" : "FAIL"}  ${msg}`);
  if (!ok) failed++;
}
process.exit(failed ? 1 : 0);
