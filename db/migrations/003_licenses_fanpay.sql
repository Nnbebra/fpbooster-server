-- db/migrations/003_licenses_fanpay.sql

-- Добавляем поля для FanPay-ключей и HWID
ALTER TABLE licenses
  ADD COLUMN duration_days INT DEFAULT 30,              -- срок действия в днях
  ADD COLUMN activated_at TIMESTAMP,                   -- когда ключ был активирован
  ADD COLUMN hwid TEXT,                                -- идентификатор железа
  ADD COLUMN hwid_locked BOOLEAN DEFAULT TRUE;         -- блокировка смены HWID
