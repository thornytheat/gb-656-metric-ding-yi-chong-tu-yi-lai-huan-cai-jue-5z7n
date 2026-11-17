-- metricgraph 合并共同基线 v2026_08
-- 两组改动都从这一份出发；合并只看这一份，不看对方分支
CREATE TABLE dim_order (
  `order_id`    BIGINT        NOT NULL,  -- fid=1001
  `order_no`    VARCHAR(64)   NOT NULL,  -- fid=1002
  `amount`      DECIMAL(18,2) NOT NULL,  -- fid=1004
  `status`      VARCHAR(16)   NOT NULL,  -- fid=1005
  `region`      VARCHAR(16)   NOT NULL,  -- fid=1007
  `legacy_code` VARCHAR(8)    NULL       -- fid=1006
);
CREATE TABLE fct_pay (
  `pay_id`      BIGINT        NOT NULL,  -- fid=2001
  `pay_amt`     DECIMAL(18,2) NOT NULL   -- fid=2003
);
