-- Runs once on first Postgres init (mounted into /docker-entrypoint-initdb.d).
-- Two separate databases keep persona psychology out of the CRM entirely.
CREATE DATABASE pulse_crm;
CREATE DATABASE pulse_channel;
