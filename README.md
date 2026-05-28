# FPBooster Server 🚀

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://python.org/)
[![Ko-fi](https://img.shields.io/badge/Support_on_Ko--fi-FF5E5B?logo=kofi&logoColor=white)](https://ko-fi.com/wexblood)

**FPBooster Server** is a high‑performance backend solution for the FPBooster ecosystem. It handles real‑time data optimization, user configuration management, and secure API delivery. The project is developed and maintained as open‑source software.

## 🛠 Technology Stack

- **Runtime:** Python 3.10+
- **Framework:** FastAPI / Flask (custom routing)
- **Database:** SQLite / PostgreSQL (via `db/` module)
- **Protocols:** HTTP/HTTPS, WebSockets
- **Architecture:** RESTful API + async workers

## 📋 Core Features

- ⚡ **Low‑latency data processing** – optimized algorithms for real‑time config pushes  
- 🔐 **Secure authentication** – JWT + crypto utilities (`utils_crypto.py`)  
- 📊 **Built‑in monitoring** – server health and metrics endpoints  
- 🔄 **Auto‑restocking system** – plugins like `AutoRestock.py` for license/resource management  
- 👥 **Creator & group management** – multi‑user roles and referrals  

## 🚀 Deployment

```bash
git clone https://github.com/Nnbebra/fpbooster-server.git
cd fpbooster-server
pip install -r requirements.txt
python server.py
