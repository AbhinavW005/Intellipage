CREATE TABLE IF NOT EXISTS processes (
    process_id BIGINT PRIMARY KEY AUTO_INCREMENT,
    windows_pid INT NOT NULL,
    process_name VARCHAR(260) NOT NULL,
    process_started_at DATETIME(6) NOT NULL,
    first_seen DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    last_seen DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_process_instance (windows_pid, process_started_at),
    INDEX idx_process_pid_last_seen (windows_pid, last_seen DESC)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS system_samples (
    system_sample_id BIGINT PRIMARY KEY AUTO_INCREMENT,
    sampled_at DATETIME(6) NOT NULL,
    memory_total_bytes BIGINT UNSIGNED NOT NULL,
    memory_available_bytes BIGINT UNSIGNED NOT NULL,
    memory_percent DECIMAL(7,3) NOT NULL,
    swap_used_bytes BIGINT UNSIGNED NOT NULL,
    swap_percent DECIMAL(7,3) NOT NULL,
    process_count INT UNSIGNED NOT NULL,
    INDEX idx_system_samples_time (sampled_at DESC)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS process_samples (
    sample_id BIGINT PRIMARY KEY AUTO_INCREMENT,
    process_id BIGINT NOT NULL,
    sampled_at DATETIME(6) NOT NULL,
    page_faults BIGINT UNSIGNED NOT NULL,
    working_set_bytes BIGINT UNSIGNED NOT NULL,
    private_bytes BIGINT UNSIGNED NOT NULL,
    cpu_percent DECIMAL(7,3) NOT NULL,
    read_bytes BIGINT UNSIGNED NOT NULL,
    write_bytes BIGINT UNSIGNED NOT NULL,
    CONSTRAINT fk_samples_process
        FOREIGN KEY (process_id) REFERENCES processes(process_id)
        ON DELETE CASCADE,
    INDEX idx_samples_process_time (process_id, sampled_at DESC),
    INDEX idx_samples_time (sampled_at DESC)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS optimization_actions (
    action_id BIGINT PRIMARY KEY AUTO_INCREMENT,
    action_kind VARCHAR(40) NOT NULL,
    target VARCHAR(1024) NOT NULL,
    started_at DATETIME(6) NOT NULL,
    duration_ms DECIMAL(14,3) NOT NULL,
    success BOOLEAN NOT NULL,
    bytes_touched BIGINT UNSIGNED NOT NULL DEFAULT 0,
    message TEXT,
    metadata_json JSON,
    INDEX idx_actions_kind_time (action_kind, started_at DESC),
    INDEX idx_actions_success_time (success, started_at DESC)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id BIGINT PRIMARY KEY AUTO_INCREMENT,
    experiment_type VARCHAR(60) NOT NULL,
    target VARCHAR(1024) NOT NULL,
    policy_name VARCHAR(60) NOT NULL,
    action_id BIGINT NOT NULL,
    baseline_latency_ms DECIMAL(14,3) NOT NULL,
    post_action_latency_ms DECIMAL(14,3) NOT NULL,
    page_faults_before BIGINT UNSIGNED NOT NULL,
    page_faults_after BIGINT UNSIGNED NOT NULL,
    working_set_before BIGINT UNSIGNED NOT NULL,
    working_set_after BIGINT UNSIGNED NOT NULL,
    reward DOUBLE NOT NULL,
    created_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_experiment_action
        FOREIGN KEY (action_id) REFERENCES optimization_actions(action_id),
    INDEX idx_experiments_policy_time (policy_name, created_at DESC),
    INDEX idx_experiments_type_time (experiment_type, created_at DESC)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS dqn_experiences (
    experience_id BIGINT PRIMARY KEY AUTO_INCREMENT,
    state_json JSON NOT NULL,
    action_index TINYINT UNSIGNED NOT NULL,
    reward DOUBLE NOT NULL,
    next_state_json JSON NOT NULL,
    done BOOLEAN NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_dqn_created (created_at DESC)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS query_metrics (
    query_metric_id BIGINT PRIMARY KEY AUTO_INCREMENT,
    query_digest CHAR(64) NOT NULL,
    statement_name VARCHAR(160) NOT NULL,
    duration_ms DECIMAL(14,3) NOT NULL,
    rows_returned BIGINT UNSIGNED NOT NULL,
    explain_text LONGTEXT,
    measured_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    INDEX idx_query_digest_time (query_digest, measured_at DESC)
) ENGINE=InnoDB;

CREATE OR REPLACE VIEW policy_performance AS
SELECT
    policy_name,
    COUNT(*) AS experiment_count,
    AVG(baseline_latency_ms) AS avg_baseline_latency_ms,
    AVG(post_action_latency_ms) AS avg_post_action_latency_ms,
    AVG(baseline_latency_ms - post_action_latency_ms) AS avg_latency_improvement_ms,
    AVG(reward) AS avg_reward,
    SUM(page_faults_after - page_faults_before) AS observed_page_fault_delta
FROM experiments
GROUP BY policy_name;

CREATE OR REPLACE VIEW latest_process_metrics AS
SELECT process_id,
       windows_pid,
       process_name,
       sampled_at,
       page_faults,
       working_set_bytes,
       private_bytes,
       cpu_percent,
       read_bytes,
       write_bytes
FROM (
    SELECT p.process_id,
           p.windows_pid,
           p.process_name,
           s.sampled_at,
           s.page_faults,
           s.working_set_bytes,
           s.private_bytes,
           s.cpu_percent,
           s.read_bytes,
           s.write_bytes,
           ROW_NUMBER() OVER (
               PARTITION BY p.process_id ORDER BY s.sampled_at DESC, s.sample_id DESC
           ) AS recency_rank
    FROM processes p
    JOIN process_samples s ON s.process_id = p.process_id
) ranked
WHERE recency_rank = 1;

