-- Roles y esquema de la memoria. Idempotente. Lo corre un superusuario por stdin, precedido de:
--   \set pw_app '<clave>'   \set pw_mig '<clave>'   \set db '<base>'
-- (las claves nunca van en argumentos de proceso ni en el repositorio).
\set ON_ERROR_STOP on
BEGIN;
SELECT 'CREATE ROLE df_memoria_owner NOLOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'df_memoria_owner') \gexec
SELECT 'CREATE ROLE df_memoria_migrador LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'df_memoria_migrador') \gexec
SELECT 'CREATE ROLE df_memoria_app LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'df_memoria_app') \gexec
ALTER ROLE df_memoria_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE df_memoria_migrador LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 3 PASSWORD :'pw_mig';
ALTER ROLE df_memoria_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT
    CONNECTION LIMIT 20 PASSWORD :'pw_app';
GRANT df_memoria_owner TO df_memoria_migrador;
CREATE SCHEMA IF NOT EXISTS memoria AUTHORIZATION df_memoria_owner;
REVOKE ALL ON SCHEMA memoria FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO df_memoria_migrador, df_memoria_app;
ALTER ROLE df_memoria_app SET search_path = memoria;
ALTER ROLE df_memoria_app SET statement_timeout = '8s';
ALTER ROLE df_memoria_migrador SET search_path = memoria;
COMMIT;
