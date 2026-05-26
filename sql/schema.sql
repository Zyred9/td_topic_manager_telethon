-- TG 小号矩阵管控平台 — 建表脚本(启动时幂等执行)
-- 字符集 utf8mb4;表名 t_ 前缀;状态值用 TINYINT;时间用 DATETIME

CREATE TABLE IF NOT EXISTS t_account (
  id            BIGINT       PRIMARY KEY AUTO_INCREMENT,
  phone         VARCHAR(32)  NOT NULL COMMENT '电话号码,唯一',
  tg_user_id    BIGINT                COMMENT 'Telegram 用户 ID(登录成功后回填)',
  tg_first_name VARCHAR(64),
  tg_last_name  VARCHAR(64),
  tg_username   VARCHAR(64),
  bio           VARCHAR(255)          COMMENT '简介',
  avatar_path   VARCHAR(255)          COMMENT '本地头像相对路径',
  import_type   TINYINT      NOT NULL COMMENT '1手机号登录 2协议号导入',
  session_path  VARCHAR(255) NOT NULL COMMENT '{phone}.session 绝对路径',
  api_id        INT                   COMMENT '协议号自带 app_id;手机号登录为 NULL 用全局',
  api_hash      VARCHAR(64)           COMMENT '协议号自带 app_hash',
  device_model  VARCHAR(64)           COMMENT '协议号自带设备型号(防封号指纹一致)',
  app_version   VARCHAR(64)           COMMENT '协议号自带客户端版本',
  lang_pack     VARCHAR(16)           COMMENT '协议号自带语言',
  two_fa        VARCHAR(64)           COMMENT '协议号 2FA 密码(来自 json/2fa.txt)',
  status        TINYINT      NOT NULL DEFAULT 0 COMMENT '0未登录 1初始化中 2等待验证码 3已登录 4离线 5需重新登录 6等待2FA',
  login_time    DATETIME              COMMENT '最近一次登录成功时间',
  persona_json  TEXT                  COMMENT 'AI 人设 JSON',
  create_time   DATETIME     NOT NULL,
  update_time   DATETIME     NOT NULL,
  UNIQUE KEY uk_phone (phone)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='小号';

CREATE TABLE IF NOT EXISTS t_schedule_task (
  id           BIGINT        PRIMARY KEY AUTO_INCREMENT,
  phone        VARCHAR(32)   NOT NULL,
  chat_ids     VARCHAR(1024) NOT NULL COMMENT '逗号分隔的群 ID',
  content      TEXT          NOT NULL,
  interval_min INT           NOT NULL COMMENT '发送间隔(分钟)',
  status       TINYINT       NOT NULL DEFAULT 0 COMMENT '0已停 1运行中',
  last_sent    DATETIME,
  create_time  DATETIME      NOT NULL,
  update_time  DATETIME      NOT NULL,
  UNIQUE KEY uk_phone (phone)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='定时发送(一号一任务)';

CREATE TABLE IF NOT EXISTS t_keyword_reply (
  id          BIGINT       PRIMARY KEY AUTO_INCREMENT,
  phone       VARCHAR(32)  NOT NULL,
  keyword     VARCHAR(128) NOT NULL,
  reply_text  TEXT         NOT NULL,
  create_time DATETIME     NOT NULL,
  INDEX idx_phone (phone)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='关键字回复(每号独立)';

CREATE TABLE IF NOT EXISTS t_ai_topic (
  id                BIGINT       PRIMARY KEY AUTO_INCREMENT,
  topic_name        VARCHAR(255) NOT NULL,
  topic_prompt      TEXT         NOT NULL COMMENT '话题主题/场景,作为 LLM system prompt',
  owner_phone       VARCHAR(32)  NOT NULL COMMENT '建群 owner 小号',
  chat_id           BIGINT       NOT NULL COMMENT 'AI 互动所在群 chatId',
  message_thread_id BIGINT       NOT NULL DEFAULT 0 COMMENT 'Forum Topic 的 threadId;0=General',
  speak_min_sec     INT          NOT NULL DEFAULT 30  COMMENT '自驱发言最小间隔(秒)',
  speak_max_sec     INT          NOT NULL DEFAULT 180 COMMENT '自驱发言最大间隔(秒)',
  reply_user_prob   INT          NOT NULL DEFAULT 30  COMMENT '真人发言后介入概率(0-100)',
  max_per_min       INT          NOT NULL DEFAULT 6   COMMENT '话题每分钟最大发言条数',
  reply_min_chars   INT          NOT NULL DEFAULT 8   COMMENT '回复汉字下限(建议)',
  reply_max_chars   INT          NOT NULL DEFAULT 25  COMMENT '回复汉字上限(建议);硬上限 50',
  filler_enabled    TINYINT      NOT NULL DEFAULT 1   COMMENT '附和短句开关 1开0关,仅自驱',
  filler_prob       INT          NOT NULL DEFAULT 10  COMMENT '自驱附和概率(0-100)',
  status            TINYINT      NOT NULL DEFAULT 0   COMMENT '0已停 1运行中',
  version           INT          NOT NULL DEFAULT 0   COMMENT '乐观锁版本号',
  create_time       DATETIME     NOT NULL,
  update_time       DATETIME     NOT NULL,
  INDEX idx_chat_thread (chat_id, message_thread_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI 话题';

CREATE TABLE IF NOT EXISTS t_ai_topic_member (
  id       BIGINT      PRIMARY KEY AUTO_INCREMENT,
  topic_id BIGINT      NOT NULL,
  phone    VARCHAR(32) NOT NULL,
  UNIQUE KEY uk (topic_id, phone)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='话题成员';

CREATE TABLE IF NOT EXISTS t_account_watch (
  id         BIGINT      PRIMARY KEY AUTO_INCREMENT,
  phone      VARCHAR(32) NOT NULL,
  chat_id    BIGINT      NOT NULL,
  chat_title VARCHAR(255) COMMENT '群名(加群时回填,展示用)',
  joined_at  DATETIME    NOT NULL COMMENT '小号加入该群的时间',
  UNIQUE KEY uk (phone, chat_id),
  INDEX idx_chat (chat_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='小号在群路由表';

CREATE TABLE IF NOT EXISTS t_admin_user (
  id          BIGINT       PRIMARY KEY AUTO_INCREMENT,
  username    VARCHAR(64)  NOT NULL,
  password    VARCHAR(128) NOT NULL COMMENT 'bcrypt 哈希',
  role        TINYINT      NOT NULL COMMENT '1超级管理员 2普通运营',
  enabled     TINYINT      NOT NULL DEFAULT 1 COMMENT '0停用 1启用',
  last_login  DATETIME,
  create_time DATETIME     NOT NULL,
  update_time DATETIME     NOT NULL,
  UNIQUE KEY uk_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='后台账号';

CREATE TABLE IF NOT EXISTS t_persona_preset (
  id          BIGINT      PRIMARY KEY AUTO_INCREMENT,
  preset_name VARCHAR(64) NOT NULL COMMENT '自定义模板名',
  preset_json TEXT        NOT NULL COMMENT '人设 JSON',
  create_time DATETIME    NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='自定义人设预置模板(内置5个写代码常量)';
