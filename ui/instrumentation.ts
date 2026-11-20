// Runs once when the server starts (Next's instrumentation convention): says
// in the server log how writes are guarded (lib/write-auth.ts), loudly when
// they are open or refused, so nobody runs either state without knowing.
export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  const { describeWriteAuth, loadWriteAuth } = await import("./lib/write-auth");
  const auth = loadWriteAuth();
  const { detail } = describeWriteAuth(auth);
  if (auth.mode === "token") {
    console.log(`portunus-ui: ${detail}`);
  } else {
    const bar = "!".repeat(72);
    console.warn(`${bar}\nportunus-ui: ${detail}\n${bar}`);
  }
}
