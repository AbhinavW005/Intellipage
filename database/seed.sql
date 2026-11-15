INSERT INTO query_metrics
    (query_digest, statement_name, duration_ms, rows_returned, explain_text)
VALUES
    (REPEAT('0', 64), 'installation_check', 0.000, 1, 'Schema installed successfully');

