/** @type {import('next').NextConfig} */
const nextConfig = {
  // Localhost-only for v1 (Grill U1 / vertical-plan.md deferred items) --
  // this app is never deployed remotely by design.
  reactStrictMode: true,
};

export default nextConfig;
