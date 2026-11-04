-- SQL walkthrough from "What Async Commit and 1PC Save in TiDB".
-- Use a fresh TiDB v8.5.3 Playground cluster and one MySQL connection.
-- Read txn_commit_mode after each COMMIT; expected modes, in order:
-- 2pc, async_commit, 1pc, 2pc, 1pc, 2pc, 1pc, 2pc, 1pc, 1pc.
-- Example: mysql --host 127.0.0.1 --port 34000 --user root < manual.sql

SET @original_batch_size = @@GLOBAL.tidb_txn_commit_batch_size;
SET GLOBAL tidb_txn_commit_batch_size = 16384;
SELECT @@GLOBAL.tidb_txn_commit_batch_size;

CREATE DATABASE txn_protocol_sql;
USE txn_protocol_sql;

CREATE TABLE lab_single (
    id BIGINT PRIMARY KEY CLUSTERED,
    v BIGINT NOT NULL,
    payload VARBINARY(65535) NOT NULL
);
INSERT INTO lab_single VALUES
    (0, 0, 'seed'), (1, 0, 'seed'), (2, 0, 'seed'), (3, 0, 'seed'),
    (4, 0, 'seed'), (5, 0, 'seed'), (6, 0, 'seed'), (7, 0, 'seed');

CREATE TABLE lab_cross LIKE lab_single;
INSERT INTO lab_cross SELECT * FROM lab_single;
SPLIT TABLE lab_cross BY (4);

SHOW TABLE lab_single REGIONS;
SHOW TABLE lab_cross REGIONS;

SET SESSION tidb_enable_1pc = 0;
SET SESSION tidb_enable_async_commit = 0;
BEGIN OPTIMISTIC;
UPDATE lab_single SET v = v + 1, payload = REPEAT('x', 32)
WHERE id BETWEEN 0 AND 7;
COMMIT;
SELECT @@tidb_last_txn_info;

SET SESSION tidb_enable_1pc = 0;
SET SESSION tidb_enable_async_commit = 1;
BEGIN OPTIMISTIC;
UPDATE lab_single SET v = v + 1, payload = REPEAT('x', 32)
WHERE id BETWEEN 0 AND 7;
COMMIT;
SELECT @@tidb_last_txn_info;

SET SESSION tidb_enable_1pc = 1;
SET SESSION tidb_enable_async_commit = 0;
BEGIN OPTIMISTIC;
UPDATE lab_single SET v = v + 1, payload = REPEAT('x', 32)
WHERE id BETWEEN 0 AND 7;
COMMIT;
SELECT @@tidb_last_txn_info;

BEGIN OPTIMISTIC;
UPDATE lab_cross SET v = v + 1, payload = REPEAT('x', 32)
WHERE id BETWEEN 0 AND 7;
COMMIT;
SELECT @@tidb_last_txn_info;

SET SESSION tidb_enable_1pc = 1;
SET SESSION tidb_enable_async_commit = 0;
SET GLOBAL tidb_txn_commit_batch_size = 16384;

-- Expected txn_commit_mode: 1pc
BEGIN OPTIMISTIC;
UPDATE lab_single SET v = v + 1, payload = REPEAT('x', 2304)
WHERE id BETWEEN 0 AND 7;
COMMIT;
SELECT @@tidb_last_txn_info;

-- Expected txn_commit_mode: 2pc
BEGIN OPTIMISTIC;
UPDATE lab_single SET v = v + 1, payload = REPEAT('x', 2336)
WHERE id BETWEEN 0 AND 7;
COMMIT;
SELECT @@tidb_last_txn_info;
SHOW TABLE lab_single REGIONS;

-- Expected txn_commit_mode: 1pc
SET GLOBAL tidb_txn_commit_batch_size = 65536;
BEGIN OPTIMISTIC;
UPDATE lab_single SET v = v + 1, payload = REPEAT('x', 1024)
WHERE id BETWEEN 0 AND 7;
COMMIT;
SELECT @@tidb_last_txn_info;

-- Expected txn_commit_mode: 2pc
SET GLOBAL tidb_txn_commit_batch_size = 4096;
BEGIN OPTIMISTIC;
UPDATE lab_single SET v = v + 1, payload = REPEAT('x', 1024)
WHERE id BETWEEN 0 AND 7;
COMMIT;
SELECT @@tidb_last_txn_info;

-- Expected txn_commit_mode: 1pc
SET GLOBAL tidb_txn_commit_batch_size = 65536;
BEGIN OPTIMISTIC;
UPDATE lab_single SET v = v + 1, payload = REPEAT('x', 1024)
WHERE id BETWEEN 0 AND 7;
COMMIT;
SELECT @@tidb_last_txn_info;

SET GLOBAL tidb_txn_commit_batch_size = 16384;
BEGIN OPTIMISTIC;
UPDATE lab_single SET v = v + 1, payload = REPEAT('x', 32768)
WHERE id = 0;
COMMIT;
SELECT @@tidb_last_txn_info;

-- Restore the value saved at the start of the walkthrough.
SET GLOBAL tidb_txn_commit_batch_size = @original_batch_size;
SELECT @@GLOBAL.tidb_txn_commit_batch_size;
