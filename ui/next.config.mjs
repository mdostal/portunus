/** @type {import('next').NextConfig} */
const nextConfig = {
  // Localhost-only for v1 (Grill U1 / vertical-plan.md deferred items) --
  // this app is never deployed remotely by design.
  reactStrictMode: true,
  // Traces a self-contained .next/standalone/server.js so the Pantheon host
  // can supervise this UI the same way it supervises Janus/Consus/Mnemosyne
  // (start: node, a single fixed entrypoint) -- see
  // .pHive/epics/portunus-l2-plugin-lifecycle/. Standalone output does NOT
  // auto-include public/ or .next/static/; both must be copied alongside
  // the standalone server manually (Next.js's own documented requirement).
  output: "standalone",
};

export default nextConfig;
