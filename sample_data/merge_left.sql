-- metricgraph 合并左分支 v2026_10
-- 与右分支各自从共同基线出发，互不知情
CREATE TABLE dim_order (
  `order_id`      BIGINT        NOT NULL,  -- fid=1001
  `order_no`      VARCHAR(64)   NOT NULL,  -- fid=1002
  `amount`        DECIMAL(20,4) NOT NULL,  -- fid=1004
  `status`        VARCHAR(32)   NOT NULL,  -- fid=1005
  `region`        VARCHAR(24)   NOT NULL,  -- fid=1007
  `order_channel` VARCHAR(16)   NOT NULL   -- fid=1012
);
CREATE TABLE fct_pay (
  `pay_id`        BIGINT        NOT NULL,  -- fid=2001
  `pay_amt`       DECIMAL(18,2) NOT NULL   -- fid=2003
);
