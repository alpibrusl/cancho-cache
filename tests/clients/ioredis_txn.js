// A transaction through ioredis, and a watch aborted by a competing writer: `node ioredis_txn.js <port>`.
const Redis = require("ioredis");
const port = parseInt(process.argv[2], 10);

function fail(what, got) {
  console.error("FAIL " + what + ": " + JSON.stringify(got));
  process.exit(1);
}

(async () => {
  const r = new Redis({ port, commandTimeout: 5000, maxRetriesPerRequest: 0 });
  const w = new Redis({ port, commandTimeout: 5000, maxRetriesPerRequest: 0 });
  await r.flushall();
  const done = await r.multi().set("io:a", "1").incr("io:a").get("io:a").exec();
  const flat = done.map(([err, v]) => (err ? "ERR" : v));
  if (JSON.stringify(flat) !== JSON.stringify(["OK", 2, "2"])) fail("multi().exec()", done);
  await r.watch("io:w");
  await w.set("io:w", "x");
  const aborted = await r.multi().set("io:w", "mine").exec();
  if (aborted !== null) fail("a competing write must make exec() answer null", aborted);
  if ((await r.get("io:w")) !== "x") fail("the aborted transaction must not have run", await r.get("io:w"));
  await r.watch("io:w");
  const ok = await r.multi().set("io:w", "mine").exec();
  if (!ok || ok[0][1] !== "OK") fail("without a competing write exec() commits", ok);
  r.disconnect();
  w.disconnect();
  console.log("ioredis txn ok");
})().catch((e) => fail("exception", String(e)));
