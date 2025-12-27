from fastapi import APIRouter, Request, HTTPException, Depends
from datetime import datetime, timedelta
import uuid

# ИСПРАВЛЕНО: Импортируем из корневого файла guards.py
from guards import admin_guard_api 

from groups import AssignGroupRequest, RevokeGroupRequest

router = APIRouter(prefix="/admin/groups", tags=["Admin Groups"])

@router.post("/assign")
async def assign_group_admin(request: Request, body: AssignGroupRequest, _=Depends(admin_guard_api)):
    """
    Выдача группы пользователю.
    """
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        # 1. Получаем ID группы
        group = await conn.fetchrow("SELECT id, access_level FROM groups WHERE slug = $1", body.group_slug)
        if not group:
            raise HTTPException(404, detail="Группа не найдена")

        # 2. Проверяем, есть ли уже такая активная запись
        existing = await conn.fetchrow("""
            SELECT id, expires_at FROM user_groups 
            WHERE user_uid = $1 AND group_id = $2
        """, body.user_uid, group['id'])

        # Логика срока действия
        duration = body.duration_days if body.duration_days else 30
        
        if existing:
            # Если уже есть - ПРОДЛЕВАЕМ
            current_expires = existing['expires_at']
            if current_expires < datetime.now():
                new_expires = datetime.now() + timedelta(days=duration)
            else:
                new_expires = current_expires + timedelta(days=duration)
            
            await conn.execute("""
                UPDATE user_groups 
                SET expires_at = $1, is_active = TRUE, granted_at = NOW()
                WHERE id = $2
            """, new_expires, existing['id'])
            
            return {"status": "extended", "new_expires": new_expires}
        
        else:
            # Если нет - СОЗДАЕМ
            new_expires = datetime.now() + timedelta(days=duration)
            await conn.execute("""
                INSERT INTO user_groups (user_uid, group_id, expires_at, is_active, granted_at)
                VALUES ($1, $2, $3, TRUE, NOW())
            """, body.user_uid, group['id'], new_expires)
            
            return {"status": "created", "new_expires": new_expires}

@router.post("/revoke")
async def revoke_group_admin(request: Request, body: RevokeGroupRequest, _=Depends(admin_guard_api)):
    """
    Снятие группы (просто ставим is_active = FALSE)
    """
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        group = await conn.fetchrow("SELECT id FROM groups WHERE slug = $1", body.group_slug)
        if not group:
            raise HTTPException(404, detail="Группа не найдена")

        await conn.execute("""
            UPDATE user_groups SET is_active = FALSE 
            WHERE user_uid = $1 AND group_id = $2
        """, body.user_uid, group['id'])

    return {"status": "revoked"}
