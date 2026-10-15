.bail on
.headers on
.mode column

-- Run with the SQLNEW CLI and :memory: from the repository root.
SELECT sqlnew_version() AS sqlnew_version, sqlnew_backend() AS backend;

-- Compressed columns retain ordinary SQL projections and aggregates.
CREATE VIRTUAL TABLE readings USING sqlnew_columnar(
  sensor INTEGER, reading REAL, message TEXT
);
INSERT INTO readings VALUES
  (7,12.5,'device ready'),
  (7,13.5,'device ready'),
  (8,NULL,'measurement unavailable');
SELECT sensor,count(*) AS samples,avg(reading) AS average
  FROM readings GROUP BY sensor ORDER BY sensor;

-- Sparse regions retrieve individual passages by coordinate.
CREATE VIRTUAL TABLE passages USING sqlnew_region(16,16,16,TEXT);
INSERT INTO passages(x,y,z,value) VALUES
  (3,4,5,'Original passage: pump inspection due Friday.'),
  (11,4,5,'Another passage: replacement filter is in stock.');
INSERT INTO passages(__sqlnew_command) VALUES('compact');
SELECT x,y,z,value FROM passages WHERE x=3 AND y=4 AND z=5;

-- Edits live beside the sealed base; readers see their current value.
BEGIN;
UPDATE passages SET value='Updated passage: pump inspection due Thursday.'
  WHERE x=3 AND y=4 AND z=5;
SELECT value AS current_passage FROM passages WHERE x=3 AND y=4 AND z=5;
ROLLBACK;
SELECT value AS restored_passage FROM passages WHERE x=3 AND y=4 AND z=5;
PRAGMA integrity_check;
