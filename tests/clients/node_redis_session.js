// A short session with node-redis (redis@4): connect and handshake, string commands, expiry, a pipeline
// and a MULTI/EXEC transaction, an error reply from the server, CLIENT SETNAME, and a reconnect after the
// socket is closed. Prints "node-redis ok" and exits 0 when every check passes.
const { createClient } = require("redis");
const port = parseInt(process.argv[2], 10);

const main = async () => {
  const eq = (a, b, what) => {
    const ja = JSON.stringify(a), jb = JSON.stringify(b);
    if (ja !== jb) { console.log("FAIL " + what + ": " + ja + " != " + jb); process.exit(1); }
  };

  let c = createClient({ socket: { port, reconnectStrategy: () => false } });
  c.on("error", (e) => { console.log("FAIL error event: " + e.message); process.exit(1); });
  await c.connect();

  // Idempotent against a warm server: clear this script's keys first.
  await c.unlink("nr:a", "nr:b", "nr:p", "nr:t", "nr:w", "nr:s");

  // Handshake happened (it sent HELLO/CLIENT and got answers) if the first command works.
  eq(await c.ping(), "PONG", "ping");
  eq(await c.set("nr:a", "1"), "OK", "set");
  eq(await c.get("nr:a"), "1", "get");
  eq(await c.incr("nr:a"), 2, "incr");
  eq(await c.incrBy("nr:a", 41), 43, "incrby");
  eq(await c.mGet(["nr:a", "nr:none"]), ["43", null], "mget keeps the holes");

  // Expiry.
  eq(await c.set("nr:b", "x", { EX: 100 }), "OK", "set EX");
  eq(await c.expire("nr:b", 100), 1, "expire");
  eq(await c.ttl("nr:b"), 100, "ttl");
  eq(await c.getDel("nr:b"), "x", "getdel");

  // Pipeline (no transaction) and a MULTI/EXEC transaction, node-redis's own shapes.
  eq(await c.multi().set("nr:p", "y").get("nr:p").expire("nr:p", 100).ttl("nr:p").exec(),
     ["OK", "y", 1, 100], "pipeline");
  eq(await c.multi().set("nr:t", "1").incr("nr:t").get("nr:t").exec(),
     ["OK", 2, "2"], "transaction");

  // An error reply from the server surfaces as an exception with Redis's text.
  let err = null;
  try { await c.set("nr:w", "not-a-number"); await c.incr("nr:w"); }
  catch (e) { err = e; }
  if (!err || !/not an integer/i.test(String(err.message))) {
    console.log("FAIL error reply: " + (err && err.message)); process.exit(1);
  }

  // CLIENT SETNAME.
  await c.clientSetName("node-redis-test");
  eq(await c.clientGetName(), "node-redis-test", "client setname");

  // Reconnect: a fresh connection works after this one is closed.
  await c.disconnect();
  c = createClient({ socket: { port, reconnectStrategy: () => false }, name: "node-redis-test" });
  c.on("error", (e) => { console.log("FAIL error event: " + e.message); process.exit(1); });
  await c.connect();
  eq(await c.get("nr:a"), "43", "reconnect and get");

  // The library-specific idiom: sendCommand with RESP types.
  eq(await c.sendCommand(["SET", "nr:s", "z"]), "OK", "sendCommand");
  eq(await c.sendCommand(["GET", "nr:s"]), "z", "sendCommand bulk");

  console.log("node-redis ok");
  await c.disconnect();
};

main().catch((e) => { console.log("FAIL " + (e && e.message)); process.exit(1); });
