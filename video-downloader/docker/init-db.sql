-- WebVideo2NAS Database Schema
-- Initialize database tables

-- Jobs table: Main download job tracking
CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url TEXT NOT NULL,
    title VARCHAR(255),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    progress INTEGER DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
    created_at TIMESTAMP DEFAULT NOW(),
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    file_size BIGINT,
    file_path TEXT,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0
);

-- Job metadata table: Additional information about jobs
CREATE TABLE IF NOT EXISTS job_metadata (
    job_id UUID PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    referer TEXT,
    headers JSONB,
    source_page TEXT,
    resolution VARCHAR(20),
    duration INTEGER,
    segment_count INTEGER,
    user_agent TEXT,
    output_subdir TEXT
);

-- Config table: System configuration
CREATE TABLE IF NOT EXISTS config (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_completed ON jobs(completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url);

-- Insert default config
INSERT INTO config (key, value) VALUES 
    ('system_version', '1.0.0'),
    ('max_concurrent_downloads', '3'),
    ('auto_cleanup_days', '30')
ON CONFLICT (key) DO NOTHING;

-- Create view for job statistics
CREATE OR REPLACE VIEW job_stats AS
SELECT 
    status,
    COUNT(*) as count,
    AVG(EXTRACT(EPOCH FROM (completed_at - started_at))) as avg_duration_seconds
FROM jobs
WHERE started_at IS NOT NULL
GROUP BY status;

-- Create function to update timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Create trigger for config table
CREATE TRIGGER update_config_updated_at 
    BEFORE UPDATE ON config
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Success message
DO $$
BEGIN
    RAISE NOTICE 'Database schema initialized successfully!';
END $$;

