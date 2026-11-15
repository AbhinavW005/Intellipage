-- Safe read-only examples for MySQL database warming.
SELECT process_name, windows_pid, last_seen
FROM processes
ORDER BY last_seen DESC
LIMIT 25;

SELECT policy_name, experiment_count, avg_reward,
       avg_baseline_latency_ms, avg_post_action_latency_ms
FROM policy_performance
ORDER BY avg_reward DESC;

SELECT action_kind, success, duration_ms, bytes_touched, started_at
FROM optimization_actions
ORDER BY started_at DESC
LIMIT 50;

SELECT sampled_at, memory_percent, memory_available_bytes,
       swap_percent, process_count
FROM system_samples
ORDER BY sampled_at DESC
LIMIT 50;
