-- metricgraph 合并右分支 v2026_11
-- 从同一份共同基线出发，和左分支互不知情
CREATE TABLE dim_order (
  `order_id`      BIGINT        NOT NULL,  -- fid=1001
  `order_code`    VARCHAR(64)   NOT NULL,  -- fid=1002
  `amount`        DECIMAL(18,2) NULL,      -- fid=1004
  `region`        VARCHAR(16)   NOT NULL,  -- fid=1007
  `status`        VARCHAR(16)   NOT NULL,  -- fid=1014
  `order_region`  VARCHAR(16)   NOT NULL,  -- fid=1013
  `legacy_code`   VARCHAR(16)   NOT NULL   -- fid=1006
);
CREATE TABLE fct_pay (
  `pay_id`        BIGINT        NOT NULL,  -- fid=2001
  `pay_amt`       DECIMAL(20,4) NOT NULL   -- fid=2003
);
