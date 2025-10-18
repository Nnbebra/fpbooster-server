-- Добавляем расширение для генерации UUID (если ещё не включено)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Добавляем уникальный UID и группу пользователю
ALTER TABLE users
  ADD COLUMN uid UUID UNIQUE DEFAULT gen_random_uuid(),
  ADD COLUMN user_group VARCHAR(32) NOT NULL DEFAULT 'Пользователь';
