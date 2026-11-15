-- Safe read-only examples for Oracle 21c database warming.
SELECT process_name, last_seen
FROM processes
ORDER BY last_seen DESC
FETCH FIRST 25 ROWS ONLY;

SELECT policy_name, experiment_count, avg_reward
FROM policy_performance
ORDER BY avg_reward DESC NULLS LAST;
