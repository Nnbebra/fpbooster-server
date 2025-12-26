from fastapi import APIRouter, Request, HTTPException, Depends
from datetime import datetime, timedelta
import uuid
from auth.guards import get_current_user
from schemas.groups import AssignGroupRequest, RevokeGroupRequest

router = APIRouter(prefix="/admin/groups", tags=["Admin Groups"])

# Вспомогательная функция синхронизации Лицензии
async def sync_license_for_user(db, user_uid: uuid.UUID, license_type: str, duration_days: int):
    """
    Создает или продлевает лицензию в основной таблице licenses.
    """
    # 1. Получаем имя пользователя (нужно для таблицы licenses)
    user = await db.fetch_one("SELECT username, email FROM users WHERE uid = :uid", {"uid": user_uid})
    if not user:
        return

    expires_date = datetime.now() + timedelta(days=duration_days)
    
    # 2. Проверяем, есть ли уже лицензия
    existing_license = await db.fetch_one(
        "SELECT * FROM licenses WHERE user_uid = :uid AND status = 'active'", 
        {"uid": user_uid}
    )

    if existing_license:
        # Обновляем существующую
        await db.execute("""
            UPDATE licenses 
            SET status = 'active', expires = :expires, license_key = :lic_type
            WHERE user_uid = :uid
        """, {
            "expires": expires_date.date(),
            "lic_type": license_type, # В вашей БД license_key используется как тип лицензии? Или status? 
                                      # Судя по SQL выше: status='active', но тип лицензии не очевиден. 
                                      # Обычно тип хранится в license_key или отдельном поле. 
                                      # ПРЕДПОЛОЖИМ, что тип лицензии это license_key или нужно добавить колонку type.
                                      # Исходя из таблицы products/purchases, свяжем это.
            "uid": user_uid
        })
    else:
        # Создаем новую
        await db.execute("""
            INSERT INTO licenses (user_uid, user_name, status, expires, created_at, duration_days, license_key)
            VALUES (:uid, :name, 'active', :expires, NOW(), :days, :l_key)
        """, {
            "uid": user_uid,
            "name": user['username'],
            "expires": expires_date.date(),
            "days": duration_days,
            "l_key": license_type # Здесь пишем 'Default', 'Plus' или 'Alpha'
        })

@router.post("/assign")
async def assign_group(request: Request, body: AssignGroupRequest, admin=Depends(get_current_user)):
    # 1. Проверка прав админа (реализуйте проверку поля is_admin или группы админа)
    # if not admin.is_admin: raise HTTPException(403)
    
    db = request.app.state.db

    # 2. Получаем данные группы
    group = await db.fetch_one("SELECT * FROM groups WHERE slug = :slug", {"slug": body.group_slug})
    if not group:
        raise HTTPException(404, detail="Группа не найдена")

    # 3. Расчет времени действия
    duration = body.duration_days if body.duration_days else group['license_duration_days']
    if body.is_forever:
        duration = 36500 # 100 лет
        expires_at = None # В БД NULL означает вечно
    else:
        expires_at = datetime.now() + timedelta(days=duration)

    # 4. Выдача группы (UPSERT)
    query = """
        INSERT INTO user_groups (user_uid, group_id, granted_by, expires_at, is_active)
        VALUES (:uid, :gid, :admin_uid, :expires, TRUE)
        ON CONFLICT (user_uid, group_id) 
        DO UPDATE SET expires_at = :expires, is_active = TRUE, granted_at = NOW();
    """
    await db.execute(query, {
        "uid": body.user_uid,
        "gid": group['id'],
        "admin_uid": admin['uid'], # Предполагаем, что admin - это dict или объект user
        "expires": expires_at
    })

    # 5. МАГИЯ: Автоматическая выдача лицензии
    if group['default_license_type']:
        await sync_license_for_user(db, body.user_uid, group['default_license_type'], duration)

    return {"status": "success", "message": f"Группа {group['name']} выдана"}

@router.post("/revoke")
async def revoke_group(request: Request, body: RevokeGroupRequest, admin=Depends(get_current_user)):
    db = request.app.state.db
    
    # 1. Находим ID группы
    group = await db.fetch_one("SELECT id, default_license_type FROM groups WHERE slug = :slug", {"slug": body.group_slug})
    if not group:
        raise HTTPException(404, detail="Группа не найдена")

    # 2. Удаляем или деактивируем запись
    await db.execute("""
        UPDATE user_groups SET is_active = FALSE 
        WHERE user_uid = :uid AND group_id = :gid
    """, {"uid": body.user_uid, "gid": group['id']})

    # 3. Снимаем лицензию, если она была привязана к этой группе
    # Логика сложная: если у человека было 2 группы дающие лицензию, удаление одной не должно убивать лицензию.
    # Для простоты: если снимаем группу, меняем статус лицензии на 'expired', если нет других активных групп.
    
    active_groups_count = await db.fetch_val("""
        SELECT COUNT(*) FROM user_groups ug
        JOIN groups g ON ug.group_id = g.id
        WHERE ug.user_uid = :uid AND ug.is_active = TRUE AND g.default_license_type IS NOT NULL
    """, {"uid": body.user_uid})

    if active_groups_count == 0:
        await db.execute("UPDATE licenses SET status = 'expired' WHERE user_uid = :uid", {"uid": body.user_uid})

    return {"status": "revoked"}

@router.get("/{user_uid}")
async def get_user_groups_admin(request: Request, user_uid: uuid.UUID):
    db = request.app.state.db
    rows = await db.fetch_all("""
        SELECT g.name, g.slug, ug.expires_at, ug.granted_at 
        FROM user_groups ug
        JOIN groups g ON ug.group_id = g.id
        WHERE ug.user_uid = :uid AND ug.is_active = TRUE
    """, {"uid": user_uid})
    return rows