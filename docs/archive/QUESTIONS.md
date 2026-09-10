here is a quick "cheat sheet" of engineering traps to keep in your back pocket whenever we design new cluster features:

The Telemetry Trap: Is this metric a point-in-time snapshot, or a cumulative counter? Snapshot metrics need active time divisors; cumulative counters need delta tracking to survive idle periods.

The State Asymmetry Trap: Does the worker node actually know what the head node is doing? In distributed systems, worker processes are often "dumb" executors. State must be explicitly broadcast, not independently guessed.

The Storage Lifecycle Trap: Where does this write live, and when does it flush? Balance RAM speed against NVMe wear, and always trap OS shutdown signals (SIGTERM) so crash recovery doesn't wipe uncommitted memory.

The Formatting Trap: What happens when the driver returns a string instead of a float? Hardware APIs love returning [Not Supported] or N/A. Every parser needs fail-soft defaults.

Whenever you get another wild idea—whether it's the UniFi PDU wall-draw integration, automated failover, or custom prompt routing—just pitch the raw concept. We can run it through these traps first, outline the requirements, and then write the code in one clean, uncompressed pass.
