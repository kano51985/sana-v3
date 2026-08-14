SELECT format(
    'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS',
    :'app_user',
    :'app_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = :'app_user'
) \gexec

SELECT format(
    'ALTER ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS',
    :'app_user',
    :'app_password'
) \gexec

SELECT format(
    'GRANT CONNECT ON DATABASE %I TO %I',
    current_database(),
    :'app_user'
) \gexec

SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_user') \gexec

SELECT format(
    'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I',
    :'app_user'
) \gexec

SELECT format(
    'GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO %I',
    :'app_user'
) \gexec

SELECT format(
    'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
    'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
    :'app_user'
) \gexec

SELECT format(
    'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
    'GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO %I',
    :'app_user'
) \gexec
