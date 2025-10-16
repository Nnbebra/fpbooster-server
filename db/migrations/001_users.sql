-- db/migrations/001_users.sql

CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  username TEXT,
  email_confirmed BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMP DEFAULT NOW(),
  last_login TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_licenses (
  user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  license_key TEXT NOT NULL REFERENCES licenses(license_key) ON DELETE CASCADE,
  PRIMARY KEY (user_id, license_key)
);

CREATE TABLE IF NOT EXISTS email_confirmations (
  user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token TEXT UNIQUE NOT NULL,
  expires TIMESTAMP NOT NULL,
  used BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMP DEFAULT NOW()
);
