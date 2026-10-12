// The redis-rs battery (issue #13): connect and handshake, string and hash commands, expiry, a
// pipeline and a MULTI/EXEC transaction (redis-rs's `atomic()`), an error reply from the server
// surfacing as an `Err` with Redis's text, `CLIENT SETNAME`, and a reconnect on a fresh connection.
// Prints "redis-rs ok" and exits 0 when every check passes.
use redis::Commands;

fn fail(what: &str, detail: String) -> ! {
    println!("FAIL {}: {}", what, detail);
    std::process::exit(1);
}

fn eq<T: std::fmt::Debug + PartialEq>(what: &str, got: T, want: T) {
    if got != want {
        fail(what, format!("{:?} != {:?}", got, want));
    }
}

fn main() {
    let port: u16 = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "6379".into())
        .parse()
        .unwrap_or_else(|_| fail("the port argument is not a number", "unreadable".into()));
    let client = redis::Client::open(format!("redis://127.0.0.1:{}", port))
        .unwrap_or_else(|e| fail("connect", e.to_string()));

    let run = |con: &mut redis::Connection| {
        // Handshake happened (redis-rs sends nothing on connect until the first command) if the
        // first command works.
        let pong: String = redis::cmd("PING").query(con).unwrap();
        eq("ping", pong, "PONG".to_string());

        // Idempotent against a warm server: clear this battery's keys first.
        let _: () = redis::cmd("UNLINK")
            .arg("rr:a")
            .arg("rr:b")
            .arg("rr:h")
            .arg("rr:p")
            .arg("rr:t")
            .arg("rr:w")
            .query(con)
            .unwrap();

        // Strings.
        let _: () = con.set("rr:a", "1").unwrap();
        let v: String = con.get("rr:a").unwrap();
        eq("get", v, "1".to_string());
        let n: i64 = con.incr("rr:a", 41).unwrap();
        eq("incr", n, 42);
        let vals: Vec<Option<String>> = con.mget(&["rr:a", "rr:none"]).unwrap();
        eq("mget keeps the holes", vals, vec![Some("42".into()), None]);

        // Expiry.
        let _: () = con.set("rr:b", "x").unwrap();
        let _: () = con.expire("rr:b", 100).unwrap();
        let ttl: i64 = con.ttl("rr:b").unwrap();
        eq("ttl", ttl, 100);

        // A pipeline (no transaction).
        let (b,): (String,) = redis::pipe()
            .set("rr:p", "y")
            .ignore()
            .get("rr:p")
            .query(con)
            .unwrap();
        eq("pipeline", b, "y".to_string());

        // A MULTI/EXEC transaction, redis-rs's own shape.
        let (tx,): (i64,) = redis::pipe()
            .atomic()
            .cmd("SET")
            .arg("rr:t")
            .arg("1")
            .ignore()
            .cmd("INCR")
            .arg("rr:t")
            .query(con)
            .unwrap();
        eq("transaction", tx, 2);

        // Hashes (the data structure beyond strings).
        let added: i64 = con.hset("rr:h", "f1", "v1").unwrap();
        eq("hset", added, 1);
        let again: i64 = con.hset("rr:h", "f1", "v1b").unwrap();
        eq("hset of a field that is there answers 0", again, 0);
        let hv: String = con.hget("rr:h", "f1").unwrap();
        eq("hget", hv, "v1b".to_string());
        let all: std::collections::HashMap<String, String> = con.hgetall("rr:h").unwrap();
        eq("hgetall", all.len(), 1);
        let missing: Option<String> = con.hget("rr:h", "zz").unwrap();
        eq("hget of a missing field is None", missing, None);

        // An error reply from the server surfaces as an Err with Redis's text.
        let _: () = con.set("rr:w", "not-a-number").unwrap();
        let err: Result<i64, _> = con.incr("rr:w", 1);
        let text = match err {
            Ok(_) => String::new(),
            Err(e) => format!("{}", e),
        };
        if !text.contains("not an integer") {
            fail("error reply", text);
        }

        // CLIENT SETNAME, by redis-rs's own method and by a raw command.
        let _: () = con.client_setname("redis-rs-test").unwrap();
        let name: String = redis::cmd("CLIENT").arg("GETNAME").query(con).unwrap();
        eq("client setname", name, "redis-rs-test".to_string());
    };

    let mut con = client
        .get_connection()
        .unwrap_or_else(|e| fail("first connection", e.to_string()));
    run(&mut con);
    drop(con);

    // Reconnect: a fresh connection works after the first one is closed.
    let mut again = client
        .get_connection()
        .unwrap_or_else(|e| fail("reconnect", e.to_string()));
    let v: String = again.get("rr:a").unwrap();
    eq("reconnect and get", v, "42".to_string());

    println!("redis-rs ok");
}
