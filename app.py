import base64
import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "bandops.db"
UPLOAD_DIR = BASE_DIR / "uploads"
ROOM_NAME = "乐队排练室"

load_dotenv(BASE_DIR / ".env")
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")

st.set_page_config(page_title="BandOps Agent", page_icon="🎸", layout="wide")


def get_client():
    return OpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
    )


def get_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def add_column_if_missing(cursor, table: str, column: str, definition: str):
    columns = {row["name"] for row in cursor.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def init_database():
    """初始化数据库，并把旧版多房间数据迁移为唯一排练室。"""
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_name TEXT NOT NULL DEFAULT '乐队排练室',
            band_name TEXT NOT NULL,
            booking_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            contact TEXT,
            contact_phone TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT '已预约',
            checkout_photo_filename TEXT,
            checkout_notes TEXT,
            checked_out_at TEXT
        )
        """
    )
    add_column_if_missing(cursor, "bookings", "contact_phone", "TEXT")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS equipment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            condition TEXT NOT NULL,
            location TEXT NOT NULL,
            photo_filename TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    # 兼容你之前的数据库：不删除记录，只补充签退字段并统一房间名称。
    add_column_if_missing(cursor, "bookings", "status", "TEXT NOT NULL DEFAULT '已预约'")
    add_column_if_missing(cursor, "bookings", "checkout_photo_filename", "TEXT")
    add_column_if_missing(cursor, "bookings", "checkout_notes", "TEXT")
    add_column_if_missing(cursor, "bookings", "checked_out_at", "TEXT")
    cursor.execute("UPDATE bookings SET room_name = ?", (ROOM_NAME,))

    connection.commit()
    connection.close()

# ---------- 排练室与设备工具 ----------
def check_room_availability(date: str, start_time: str, end_time: str) -> dict:
    try:
        datetime.strptime(date, "%Y-%m-%d")
        start = datetime.strptime(start_time, "%H:%M")
        end = datetime.strptime(end_time, "%H:%M")
        if start >= end:
            return {"success": False, "message": "结束时间必须晚于开始时间。"}
    except ValueError:
        return {"success": False, "message": "日期格式为 YYYY-MM-DD，时间格式为 HH:MM。"}

    connection = get_connection()
    conflicts = connection.execute(
        """
        SELECT id, band_name, start_time, end_time
        FROM bookings
        WHERE booking_date = ? AND status != '已取消' AND start_time < ? AND end_time > ?
        ORDER BY start_time
        """,
        (date, end_time, start_time),
    ).fetchall()
    connection.close()

    if conflicts:
        return {
            "success": True,
            "可预约": False,
            "排练室": ROOM_NAME,
            "日期": date,
            "时间段": f"{start_time}-{end_time}",
            "冲突预约": [dict(row) for row in conflicts],
        }
    return {
        "success": True,
        "可预约": True,
        "排练室": ROOM_NAME,
        "日期": date,
        "时间段": f"{start_time}-{end_time}",
    }

def create_rehearsal_booking(
    band_name: str,
    date: str,
    start_time: str,
    end_time: str,
    contact: str,
    contact_phone: str,
    notes: str = "",
) -> dict:
    if not contact.strip() or not contact_phone.strip():
        return {
            "success": False,
            "message": "创建预约必须填写联系人姓名和联系电话。",
        }

    phone_digits = re.sub(r"\D", "", contact_phone)
    if not 6 <= len(phone_digits) <= 20:
        return {
            "success": False,
            "message": "联系电话应包含 6 至 20 位数字。",
        }

    availability = check_room_availability(date, start_time, end_time)

    if not availability.get("success"):
        return availability

    if not availability["可预约"]:
        return {
            "success": False,
            "message": "唯一排练室在这个时段已被预约，无法重复预约。",
            "冲突预约": availability["冲突预约"],
        }

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        """
        INSERT INTO bookings (
            room_name, band_name, booking_date, start_time, end_time,
            contact, contact_phone, notes, created_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ROOM_NAME,
            band_name,
            date,
            start_time,
            end_time,
            contact,
            phone_digits,
            notes,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "已预约",
        ),
    )

    booking_id = cursor.lastrowid
    connection.commit()
    connection.close()

    return {
        "success": True,
        "预约编号": booking_id,
        "乐队": band_name,
        "排练室": ROOM_NAME,
        "时间": f"{date} {start_time}-{end_time}",
        "联系人": contact,
        "联系电话": phone_digits,
        "状态": "待签退",
    }
def list_bookings(date: str) -> dict:
    """查看某一天的全部预约记录。"""
    connection = get_connection()

    rows = connection.execute(
        """
        SELECT id, band_name, start_time, end_time, contact, contact_phone, status,
               checkout_photo_filename, checked_out_at
        FROM bookings
        WHERE booking_date = ?
        ORDER BY start_time
        """,
        (date,),
    ).fetchall()

    connection.close()

    return {
        "success": True,
        "日期": date,
        "预约记录": [dict(row) for row in rows],
        "记录数量": len(rows),
    }

def cancel_booking(booking_id: int, reason: str = "") -> dict:
    """取消尚未签退的预约，但保留记录作为历史。"""
    connection = get_connection()

    booking = connection.execute(
        "SELECT * FROM bookings WHERE id = ?",
        (booking_id,),
    ).fetchone()

    if not booking:
        connection.close()
        return {
            "success": False,
            "message": f"没有找到预约编号 {booking_id}。",
        }

    if booking["status"] == "已取消":
        connection.close()
        return {
            "success": False,
            "message": f"预约编号 {booking_id} 已经取消。",
        }

    if booking["status"] == "已签退":
        connection.close()
        return {
            "success": False,
            "message": "已签退的历史预约不能取消。",
        }

    cancel_note = f"已取消：{reason}" if reason else "已取消"

    connection.execute(
        "UPDATE bookings SET status = '已取消', notes = ? WHERE id = ?",
        (cancel_note, booking_id),
    )
    connection.commit()
    connection.close()

    return {
        "success": True,
        "预约编号": booking_id,
        "乐队": booking["band_name"],
        "时间": f"{booking['booking_date']} {booking['start_time']}-{booking['end_time']}",
        "状态": "已取消",
    }

def update_booking(
    booking_id: int,
    band_name: str = "",
    date: str = "",
    start_time: str = "",
    end_time: str = "",
    contact: str = "",
    contact_phone: str = "",
    notes: str = "",
) -> dict:
    """修改待签退预约；改时间时会检查排练室冲突。"""
    connection = get_connection()

    booking = connection.execute(
        "SELECT * FROM bookings WHERE id = ?",
        (booking_id,),
    ).fetchone()

    if not booking:
        connection.close()
        return {
            "success": False,
            "message": f"没有找到预约编号 {booking_id}。",
        }

    if booking["status"] != "已预约":
        connection.close()
        return {
            "success": False,
            "message": f"当前状态为“{booking['status']}”的预约不能修改。",
        }

    updated = {
        "band_name": band_name.strip() or booking["band_name"],
        "date": date or booking["booking_date"],
        "start_time": start_time or booking["start_time"],
        "end_time": end_time or booking["end_time"],
        "contact": contact.strip() or booking["contact"],
        "contact_phone": contact_phone.strip() or booking["contact_phone"],
        "notes": notes.strip() or booking["notes"] or "",
    }

    if not updated["contact"] or not updated["contact_phone"]:
        connection.close()
        return {
            "success": False,
            "message": "预约必须保留联系人姓名和联系电话。",
        }
    phone_digits = re.sub(r"\D", "", updated["contact_phone"])
    if not 6 <= len(phone_digits) <= 20:
        connection.close()
        return {
            "success": False,
            "message": "联系电话应包含 6 至 20 位数字。",
        }

    try:
        datetime.strptime(updated["date"], "%Y-%m-%d")

        if (
            datetime.strptime(updated["start_time"], "%H:%M")
            >= datetime.strptime(updated["end_time"], "%H:%M")
        ):
            connection.close()
            return {
                "success": False,
                "message": "结束时间必须晚于开始时间。",
            }
    except ValueError:
        connection.close()
        return {
            "success": False,
            "message": "日期格式为 YYYY-MM-DD，时间格式为 HH:MM。",
        }

    conflict = connection.execute(
        """
        SELECT id, band_name, start_time, end_time
        FROM bookings
        WHERE id != ?
          AND status != '已取消'
          AND booking_date = ?
          AND start_time < ?
          AND end_time > ?
        """,
        (
            booking_id,
            updated["date"],
            updated["end_time"],
            updated["start_time"],
        ),
    ).fetchone()

    if conflict:
        connection.close()
        return {
            "success": False,
            "message": "修改后的时段与另一条预约冲突。",
            "冲突预约": dict(conflict),
        }
    connection.execute(
        """
        UPDATE bookings
        SET band_name = ?,
            booking_date = ?,
            start_time = ?,
            end_time = ?,
            contact = ?,
            contact_phone = ?,
            notes = ?
        WHERE id = ?
        """,
        (
            updated["band_name"],
            updated["date"],
            updated["start_time"],
            updated["end_time"],
            updated["contact"],
            phone_digits,
            updated["notes"],
            booking_id,
        ),
    )

    connection.commit()
    connection.close()

    return {
        "success": True,
        "预约编号": booking_id,
        "乐队": updated["band_name"],
        "时间": f"{updated['date']} {updated['start_time']}-{updated['end_time']}",
        "联系人": updated["contact"],
        "联系电话": phone_digits,
        "状态": "已修改",
    }


def checkout_rehearsal(booking_id: int, photo_filename: str, notes: str = "") -> dict:
    """签退必须附带整理后的排练室照片。"""
    if not photo_filename:
        return {"success": False, "message": "签退必须上传一张整理后的排练室照片。"}

    connection = get_connection()
    booking = connection.execute(
        "SELECT * FROM bookings WHERE id = ?", (booking_id,)
    ).fetchone()
    if not booking:
        connection.close()
        return {"success": False, "message": f"没有找到预约编号 {booking_id}。"}
    if booking["status"] == "已签退":
        connection.close()
        return {"success": False, "message": f"预约编号 {booking_id} 已经签退过。"}

    checked_out_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection.execute(
        """
        UPDATE bookings
        SET status = '已签退', checkout_photo_filename = ?,
            checkout_notes = ?, checked_out_at = ?
        WHERE id = ?
        """,
        (photo_filename, notes, checked_out_at, booking_id),
    )
    connection.commit()
    connection.close()
    return {
        "success": True,
        "预约编号": booking_id,
        "乐队": booking["band_name"],
        "签退时间": checked_out_at,
        "整理照片": photo_filename,
        "状态": "已签退",
    }


def register_equipment(
    name: str,
    category: str,
    condition: str,
    location: str,
    photo_filename: str = "",
    notes: str = "",
) -> dict:
    connection = get_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO equipment (name, category, condition, location, photo_filename, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (name, category, condition, location, photo_filename, notes, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    equipment_id = cursor.lastrowid
    connection.commit()
    connection.close()
    return {
        "success": True,
        "设备编号": equipment_id,
        "设备名称": name,
        "类别": category,
        "状态": condition,
        "存放位置": location,
        "照片文件": photo_filename or "未上传照片",
    }

def search_equipment(query: str) -> dict:
    keyword = f"%{query}%"
    connection = get_connection()
    rows = connection.execute(
        """
        SELECT id, name, category, condition, location, photo_filename, notes
        FROM equipment
        WHERE name LIKE ? OR category LIKE ? OR location LIKE ? OR notes LIKE ?
        ORDER BY id DESC
        """,
        (keyword, keyword, keyword, keyword),
    ).fetchall()
    connection.close()
    return {"搜索关键词": query, "结果": [dict(row) for row in rows], "结果数量": len(rows)}


TOOL_FUNCTIONS = {
    "check_room_availability": check_room_availability,
    "create_rehearsal_booking": create_rehearsal_booking,
    "list_bookings": list_bookings,
    "cancel_booking": cancel_booking,
    "update_booking": update_booking,
    "checkout_rehearsal": checkout_rehearsal,
    "register_equipment": register_equipment,
    "search_equipment": search_equipment,
}

TOOLS = [
    {
        "type": "function",
        "name": "check_room_availability",
        "description": "查询唯一排练室在指定时段是否可预约。",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "格式 YYYY-MM-DD",
                },
                "start_time": {
                    "type": "string",
                    "description": "格式 HH:MM",
                },
                "end_time": {
                    "type": "string",
                    "description": "格式 HH:MM",
                },
            },
            "required": ["date", "start_time", "end_time"],
        },
    },
    {
        "type": "function",
        "name": "create_rehearsal_booking",
        "description": "为乐队预约唯一排练室。只有确认空闲后才可调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "band_name": {
                    "type": "string",
                    "description": "乐队名称",
                },
                "date": {
                    "type": "string",
                    "description": "格式 YYYY-MM-DD",
                    },
                "start_time": {
                    "type": "string",
                    "description": "格式 HH:MM",
                },
                "end_time": {
                    "type": "string",
                    "description": "格式 HH:MM",
                },
                "contact": {
                    "type": "string",
                    "description": "联系人姓名",
                },
                "contact_phone": {
                    "type": "string",
                    "description": "联系人电话",
                },
                "notes": {
                    "type": "string",
                    "description": "预约备注",
                },
            },
            "required": [
                "band_name",
                "date",
                "start_time",
                "end_time",
                "contact",
                "contact_phone",
            ],
        },
    },
    {
        "type": "function",
        "name": "list_bookings",
        "description": "查询某一天唯一排练室的全部预约和签退状态。",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "格式 YYYY-MM-DD",
                },
            },
            "required": ["date"],
        },
    },
    {
        "type": "function",
        "name": "cancel_booking",
        "description": "按预约编号取消一条待签退预约；取消记录会保留。",
        "parameters": {
            "type": "object",
            "properties": {
                "booking_id": {
                    "type": "integer",
                    "description": "预约编号",
                    },
                "reason": {
                    "type": "string",
                    "description": "取消原因，可选",
                },
            },
            "required": ["booking_id"],
        },
    },
    {
        "type": "function",
        "name": "update_booking",
        "description": "按预约编号修改乐队、时间、联系人或联系电话。只提供需要修改的字段；修改时间会检查冲突。",
        "parameters": {
            "type": "object",
            "properties": {
                "booking_id": {
                    "type": "integer",
                    "description": "预约编号",
                },
                "band_name": {
                    "type": "string",
                    "description": "新的乐队名称",
                },
                "date": {
                    "type": "string",
                    "description": "新的日期，格式 YYYY-MM-DD",
                },
                "start_time": {
                    "type": "string",
                    "description": "新的开始时间，格式 HH:MM",
                },
                "end_time": {
                    "type": "string",
                    "description": "新的结束时间，格式 HH:MM",
                },
                "contact": {
                    "type": "string",
                    "description": "新的联系人姓名",
                },
                "contact_phone": {
                    "type": "string",
                    "description": "新的联系电话",
                },
                "notes": {
                    "type": "string",
                    "description": "新的备注",
                  },
            },
            "required": ["booking_id"],
        },
    },
    {
        "type": "function",
        "name": "checkout_rehearsal",
        "description": "为一条预约签退。必须使用上下文中提供的整理照片文件名。",
        "parameters": {
            "type": "object",
            "properties": {
                "booking_id": {
                    "type": "integer",
                    "description": "预约编号",
                },
                "photo_filename": {
                    "type": "string",
                    "description": "签退整理照片文件名",
                },
                "notes": {
                    "type": "string",
                    "description": "签退备注",
                },
            },
            "required": ["booking_id", "photo_filename"],
        },
    },
    {
        "type": "function",
        "name": "register_equipment",
        "description": "登记设备；上传设备照片时，必须使用上下文中的设备照片文件名。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "设备名称",
                },
                "category": {
                    "type": "string",
                    "description": "设备类别",
                },
                "condition": {
                    "type": "string",
                    "description": "设备状态",
                },
                "location": {
                    "type": "string",
                    "description": "存放位置",
                },
                "photo_filename": {
                    "type": "string",
                    "description": "设备照片文件名",
                },
                "notes": {
                    "type": "string",
                    "description": "设备备注",
                },
            },
            "required": ["name", "category", "condition", "location"],
        },
    },
    {
        "type": "function",
        "name": "search_equipment",
        "description": "按名称、类别、位置或备注查询设备。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词",
                },
            },
            "required": ["query"],
        },
    },
]



def extract_explicit_booking_fields(message: str) -> dict:
    text = message.replace("：", ":").replace("－", "-").replace("—", "-")
    fields = {}

    band = re.search(r"(?:帮|给|为)([^，。,\n]*?乐队)", text)
    if band:
        fields["band_name"] = band.group(1).strip()

    contact = re.search(r"联系人\s*(?:是|:)?\s*([^，。,\n]+)", text)
    if contact:
        fields["contact"] = contact.group(1).strip()

    phone = re.search(
        r"(?:联系电话|联系人电话|电话|手机号)\s*(?:是|:)?\s*([+\d\s()\-]{6,25})",
        text,
    )
    if not phone:
        phone = re.search(r"(?<!\d)(?:\+?\d[\d\s()\-]{5,23}\d)(?!\d)", text)

    if phone:
        fields["contact_phone"] = phone.group(1).strip()

    booking_id = re.search(r"(?:预约编号|预约号|编号|#)\s*(\d+)", text)
    if booking_id:
        fields["booking_id"] = int(booking_id.group(1))

    date = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?", text)
    if date:
        fields["date"] = (
            f"{date.group(1)}-{int(date.group(2)):02d}-{int(date.group(3)):02d}"
        )

    time_range = re.search(
        r"(\d{1,2}:\d{2})\s*(?:到|-|至)\s*(\d{1,2}:\d{2})",
        text,
    )
    if time_range:
        fields["start_time"] = time_range.group(1).zfill(5)
        fields["end_time"] = time_range.group(2).zfill(5)

    return fields

def analyze_equipment_photo(file_bytes: bytes, mime_type: str) -> str:
    image_base64 = base64.b64encode(file_bytes).decode("utf-8")
    response = get_client().responses.create(
        model=MODEL,
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": "识别图片里的乐队设备。说明名称、类别和可见状态；不确定时明确说明。"},
            {"type": "input_image", "image_url": f"data:{mime_type};base64,{image_base64}"},
        ]}],
    )
    return response.output_text

def save_uploaded_file(uploaded_file, prefix: str) -> str:
    data = uploaded_file.getvalue()
    suffix = Path(uploaded_file.name).suffix.lower() or ".jpg"
    filename = f"{prefix}_{uuid.uuid4().hex}{suffix}"
    (UPLOAD_DIR / filename).write_bytes(data)
    return filename


def safe_photo_path(filename: str | None):
    if not filename:
        return None
    candidate = UPLOAD_DIR / Path(filename).name
    return candidate if candidate.is_file() else None


def format_success(result: dict, arguments: dict) -> str:
    if "预约编号" in result and result.get("状态") == "待签退":
        return (
            "预约已创建成功 ✅\n\n"
            f"- 预约编号：{result['预约编号']}\n"
            f"- 乐队：{result['乐队']}\n"
            f"- 排练室：{result['排练室']}\n"
            f"- 时间：{result['时间']}\n"
            f"- 联系人：{result['联系人']}\n"
            f"- 电话：{result['联系电话']}\n\n"
            "排练结束后，请上传整理好的排练室照片，并发送“为预约编号 X 签退”。"
        )
    if result.get("状态") == "已取消":
        return (
            "预约已取消 ✅\n\n"
            f"- 预约编号：{result['预约编号']}\n"
            f"- 乐队：{result['乐队']}\n"
            f"- 原预约时间：{result['时间']}\n\n"
            "取消记录已保留，不会再占用排练室时段。"
        )
    if result.get("状态") == "已修改":
        return (
            "预约已修改 ✅\n\n"
            f"- 预约编号：{result['预约编号']}\n"
            f"- 乐队：{result['乐队']}\n"
            f"- 时间：{result['时间']}\n"
            f"- 联系人：{result['联系人']}\n"
            f"- 电话：{result['联系电话']}"
        )
    if result.get("状态") == "已签退":
        return (
            "签退完成 ✅\n\n"
            f"- 预约编号：{result['预约编号']}\n"
            f"- 乐队：{result['乐队']}\n"
            f"- 签退时间：{result['签退时间']}\n"
            f"- 整理照片：{result['整理照片']}"
        )
    if "设备编号" in result:
        return (
            "设备已登记 ✅\n\n"
            f"- 设备编号：{result['设备编号']}\n"
            f"- 名称：{result['设备名称']}\n"
            f"- 类别：{result['类别']}\n"
            f"- 状态：{result['状态']}\n"
            f"- 存放位置：{result['存放位置']}"
        )
    return ""

def run_agent(user_message: str, photo_context: str, history: str):
    explicit_booking_fields = extract_explicit_booking_fields(user_message)
    instructions = f"""
你是 BandOps 乐队运营 Agent，负责一个排练室的预约、签退和设备管理。

规则：
1. 回答使用中文，简洁、准确。
2. 创建预约必须有：乐队名、日期、开始时间、结束时间、联系人姓名、联系电话。
   缺少任何一项时，必须先向用户询问，不能自行编造。
   信息齐全时，先调用 check_room_availability 查询空档，再调用 create_rehearsal_booking 创建预约。
3. 用户明确提供的乐队名、日期、时间、联系人和联系电话必须原样保留，不能改写、替换或猜测。
4. 用户要求取消预约时，必须先确认预约编号；拿到编号后调用 cancel_booking。
   取消只改变预约状态，不删除历史记录。
5. 用户要求修改预约的时间、乐队名、联系人或电话时，必须先确认预约编号；
   拿到编号后调用 update_booking。修改时间时系统会自动检查是否冲突。
6. 用户签退时，必须有预约编号和“签退整理照片文件名”。
   两项缺少任一项时，询问用户；信息齐全后调用 checkout_rehearsal。
7. 用户登记设备时，如有上传设备照片，优先根据照片识别设备信息；再调用 register_equipment。
8. 用户查询设备时，调用 search_equipment。
9. 不要声称已经完成预约、取消、修改、签退或登记，除非工具返回 success 为 true。
"""
    full_input = f"最近对话：\n{history}\n\n当前用户请求：\n{user_message}"
    if photo_context:
        full_input += f"\n\n图片上下文：\n{photo_context}"

    response = get_client().responses.create(model=MODEL, instructions=instructions, input=full_input, tools=TOOLS)
    tool_logs = []
    for _ in range(5):
        calls = [item for item in response.output if item.type == "function_call"]
        if not calls:
            return response.output_text, tool_logs
        tool_outputs = []
        fixed_answer = ""
        for call in calls:
            arguments = json.loads(call.arguments)

        # 查询空档：优先使用用户在本轮消息中明确说出的日期和时间
        if call.name == "check_room_availability":
            arguments.update(
                {
                    key: explicit_booking_fields[key]
                    for key in ("date", "start_time", "end_time")
                    if key in explicit_booking_fields
                }
            )

        # 创建预约：乐队、时间、联系人和电话以用户原话为准
        if call.name == "create_rehearsal_booking":
            arguments.update(
                {
                    key: explicit_booking_fields[key]
                    for key in (
                        "band_name",
                        "date",
                        "start_time",
                        "end_time",
                        "contact",
                        "contact_phone",
                    )
                    if key in explicit_booking_fields
                }
            )

            if (
                "contact" not in explicit_booking_fields
                or "contact_phone" not in explicit_booking_fields
            ):
                result = {
                    "success": False,
                    "message": "创建预约必须由用户明确提供联系人姓名和联系电话。",
                }
                tool_logs.append(
                    {"调用工具": call.name, "参数": arguments, "结果": result}
                )
                tool_outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": json.dumps(result, ensure_ascii=False),
                    }
                )
                continue

                    # 取消或修改预约：预约编号必须来自用户原话，不能由 AI 猜测
        if call.name in {"cancel_booking", "update_booking"}:
            if "booking_id" not in explicit_booking_fields:
                result = {
                    "success": False,
                    "message": "取消或修改预约时，必须由用户明确提供预约编号。",
                }
                tool_logs.append(
                    {"调用工具": call.name, "参数": arguments, "结果": result}
                )
                tool_outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": json.dumps(result, ensure_ascii=False),
                    }
                )
                continue

            arguments["booking_id"] = explicit_booking_fields["booking_id"]

            # 修改预约：覆盖 AI 可能改写的字段
            if call.name == "update_booking":
                arguments.update(
                    {
                        key: explicit_booking_fields[key]
                        for key in (
                            "band_name",
                            "date",
                            "start_time",
                            "end_time",
                            "contact",
                            "contact_phone",
                        )
                        if key in explicit_booking_fields
                    }
                )

            result = TOOL_FUNCTIONS[call.name](**arguments)
            tool_logs.append({"调用工具": call.name, "参数": arguments, "结果": result})

            tool_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(result, ensure_ascii=False),
                }
            )

            if result.get("success"):
                formatted = format_success(result, arguments)
                if formatted:
                    fixed_answer = formatted
                   
        if fixed_answer:
            return fixed_answer, tool_logs
       
        response = get_client().responses.create(
            model=MODEL, instructions=instructions, input=list(response.output) + tool_outputs, tools=TOOLS
        )
    return "本次操作调用工具次数过多，请换一种更具体的说法。", tool_logs


# 重新定义 Agent 循环：确保每一个 DeepSeek 工具调用都有对应的回传结果。
def run_agent(user_message: str, photo_context: str, history: str):
    hidden_replies = {
        "哈哈": "张文奇好帅，但是葛佳玲更美。",
        "wym": "牛宝宝是全世界最好的牛宝宝。",
        "zyc": "猪宝宝是全世界最好的猪宝宝。",
    }
    hidden_reply = hidden_replies.get(user_message.strip().lower())
    if hidden_reply:
        return hidden_reply, []

    explicit_booking_fields = extract_explicit_booking_fields(user_message)
    instructions = """
你是 BandOps 乐队运营 Agent，负责一个排练室的预约、签退和设备管理。

规则：
1. 回答使用中文，简洁、准确。
2. 创建预约必须有：乐队名、日期、开始时间、结束时间、联系人姓名、联系电话。缺少任何一项时必须先询问，不能自行编造。信息齐全时，先查询空档，再创建预约。
3. 用户明确提供的乐队名、日期、时间、联系人和联系电话必须原样保留，不能改写、替换或猜测。
4. 用户取消预约时必须有预约编号，然后调用 cancel_booking；取消只改变状态，不删除历史记录。
5. 用户修改预约的时间、乐队名、联系人或电话时必须有预约编号，然后调用 update_booking。
6. 用户签退时必须有预约编号和签退整理照片文件名，然后调用 checkout_rehearsal。
7. 用户登记设备时，如有上传设备照片，优先根据照片识别结果登记设备；查询设备时调用 search_equipment。
8. 不要声称已经完成预约、取消、修改、签退或登记，除非工具返回 success 为 true。
"""

    full_input = f"最近对话：\n{history}\n\n当前用户请求：\n{user_message}"
    if photo_context:
        full_input += f"\n\n图片上下文：\n{photo_context}"

    response = get_client().responses.create(
        model=MODEL,
        instructions=instructions,
        input=full_input,
        tools=TOOLS,
    )
    tool_logs = []

    for _ in range(5):
        calls = [item for item in response.output if item.type == "function_call"]
        if not calls:
            return response.output_text, tool_logs

        tool_outputs = []
        fixed_answer = ""

        for call in calls:
            arguments = json.loads(call.arguments)

            if call.name == "check_room_availability":
                arguments.update(
                    {
                        key: explicit_booking_fields[key]
                        for key in ("date", "start_time", "end_time")
                        if key in explicit_booking_fields
                    }
                )

            if call.name == "create_rehearsal_booking":
                arguments.update(
                    {
                        key: explicit_booking_fields[key]
                        for key in (
                            "band_name",
                            "date",
                            "start_time",
                            "end_time",
                            "contact",
                            "contact_phone",
                        )
                        if key in explicit_booking_fields
                    }
                )

                if (
                    "contact" not in explicit_booking_fields
                    or "contact_phone" not in explicit_booking_fields
                ):
                    result = {
                        "success": False,
                        "message": "创建预约必须由用户明确提供联系人姓名和联系电话。",
                    }
                    tool_logs.append(
                        {"调用工具": call.name, "参数": arguments, "结果": result}
                    )
                    tool_outputs.append(
                        {
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": json.dumps(result, ensure_ascii=False),
                        }
                    )
                    continue

            if call.name in {"cancel_booking", "update_booking"}:
                if "booking_id" not in explicit_booking_fields:
                    result = {
                        "success": False,
                        "message": "取消或修改预约时，必须由用户明确提供预约编号。",
                    }
                    tool_logs.append(
                        {"调用工具": call.name, "参数": arguments, "结果": result}
                    )
                    tool_outputs.append(
                        {
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": json.dumps(result, ensure_ascii=False),
                        }
                    )
                    continue

                arguments["booking_id"] = explicit_booking_fields["booking_id"]

            if call.name == "update_booking":
                arguments.update(
                    {
                        key: explicit_booking_fields[key]
                        for key in (
                            "band_name",
                            "date",
                            "start_time",
                            "end_time",
                            "contact",
                            "contact_phone",
                        )
                        if key in explicit_booking_fields
                    }
                )

            result = TOOL_FUNCTIONS[call.name](**arguments)
            tool_logs.append({"调用工具": call.name, "参数": arguments, "结果": result})
            tool_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(result, ensure_ascii=False),
                }
            )

            if result.get("success"):
                formatted = format_success(result, arguments)
                if formatted:
                    fixed_answer = formatted

        # 本轮每个工具调用都已有结果后，才能结束；否则要把所有结果交回模型。
        if fixed_answer:
            return fixed_answer, tool_logs

        response = get_client().responses.create(
            model=MODEL,
            instructions=instructions,
            input=list(response.output) + tool_outputs,
            tools=TOOLS,
        )

    return "本次操作调用工具次数过多，请换一种更具体的说法。", tool_logs


init_database()
UPLOAD_DIR.mkdir(exist_ok=True)

if "messages" not in st.session_state:
    st.session_state.messages = []
if "equipment_photo" not in st.session_state:
    st.session_state.equipment_photo = {}
if "checkout_photo" not in st.session_state:
    st.session_state.checkout_photo = {}

st.title("🎸 BandOps Agent")
st.caption("唯一排练室预约与签退 · 设备登记与照片资产库")

with st.sidebar:
    st.header("可用功能")
    st.markdown("""
- 唯一排练室的预约与空档查询
- 签退时归档整理后的排练室照片
- 设备照片识别、登记和图片回看
""")
    st.divider()
    device_file = st.file_uploader("上传设备照片", type=["jpg", "jpeg", "png", "webp"], key="device_file")
    if device_file:
        signature = hashlib.sha256(device_file.getvalue()).hexdigest()
        if st.session_state.equipment_photo.get("signature") != signature:
            with st.spinner("首次上传，正在识别设备照片……"):
                filename = save_uploaded_file(device_file, "equipment")
                analysis = analyze_equipment_photo(device_file.getvalue(), device_file.type or "image/jpeg")
                st.session_state.equipment_photo = {"signature": signature, "filename": filename, "analysis": analysis}
        st.image(device_file, caption="当前设备照片")

    checkout_file = st.file_uploader("上传签退整理照片", type=["jpg", "jpeg", "png", "webp"], key="checkout_file")
    if checkout_file:
        signature = hashlib.sha256(checkout_file.getvalue()).hexdigest()
        if st.session_state.checkout_photo.get("signature") != signature:
            filename = save_uploaded_file(checkout_file, "checkout")
            st.session_state.checkout_photo = {"signature": signature, "filename": filename}
        st.image(checkout_file, caption="当前签退整理照片")

    if st.button("清空对话"):
        st.session_state.messages = []
        st.rerun()

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message.get("logs"):
            with st.expander("查看本次 Agent 工具调用"):
                for log in message["logs"]:
                    st.json(log)

st.info("试试：帮 cordial 乐队预约 2026-09-26 19:00 到 22:00，联系人小王，电话 13800138000。")
prompt = st.chat_input("预约、签退、登记设备，或查询设备照片")

if prompt:
    if not os.getenv("DEEPSEEK_API_KEY"):
        st.error("未检测到 DeepSeek API Key，请检查与 app.py 同级的 .env 文件。")
        st.stop()
    history = "\n".join(f"{item['role']}：{item['content']}" for item in st.session_state.messages[-6:])
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    contexts = []
    if st.session_state.equipment_photo:
        contexts.append(
            "设备照片文件名：{0}\n设备图片识别结果：{1}".format(
                st.session_state.equipment_photo["filename"], st.session_state.equipment_photo["analysis"]
            )
        )
    if st.session_state.checkout_photo:
        contexts.append(f"签退整理照片文件名：{st.session_state.checkout_photo['filename']}")

    try:
        with st.chat_message("assistant"):
            with st.spinner("BandOps Agent 正在处理……"):
                answer, logs = run_agent(prompt, "\n\n".join(contexts), history)
            st.markdown(answer)
            if logs:
                with st.expander("查看本次 Agent 工具调用"):
                    for log in logs:
                        st.json(log)
        st.session_state.messages.append({"role": "assistant", "content": answer, "logs": logs})
    except Exception as error:
        st.error(f"运行失败：{error}")

st.divider()
booking_tab, equipment_tab, checkout_tab = st.tabs(["预约记录", "设备照片库", "签退照片归档"])

with booking_tab:
    connection = get_connection()
    bookings = connection.execute(
        """SELECT id, band_name, booking_date, start_time, end_time, contact, contact_phone, status, checked_out_at
           FROM bookings ORDER BY booking_date DESC, start_time DESC"""
    ).fetchall()
    connection.close()
    st.dataframe([dict(row) for row in bookings], use_container_width=True)

with equipment_tab:
    connection = get_connection()
    equipment = connection.execute(
        """SELECT id, name, category, condition, location, photo_filename, created_at
           FROM equipment ORDER BY id DESC"""
    ).fetchall()
    connection.close()
    if not equipment:
        st.info("还没有登记设备。")
    else:
        records = [dict(row) for row in equipment]
        st.dataframe(records, use_container_width=True)
        selected = st.selectbox("选择设备查看原始登记照片", records, format_func=lambda item: f"#{item['id']} · {item['name']} · {item['location']}")
        photo_path = safe_photo_path(selected["photo_filename"])
        if photo_path:
            st.image(str(photo_path), caption=f"{selected['name']} 的登记照片")
        else:
            st.warning("这条设备记录没有可用照片。")

with checkout_tab:
    connection = get_connection()
    checkouts = connection.execute(
        """SELECT id, band_name, booking_date, checked_out_at, checkout_photo_filename, checkout_notes
           FROM bookings WHERE checkout_photo_filename IS NOT NULL ORDER BY checked_out_at DESC"""
    ).fetchall()
    connection.close()
    if not checkouts:
        st.info("还没有签退照片。")
    else:
        records = [dict(row) for row in checkouts]
        st.dataframe(records, use_container_width=True)
        selected = st.selectbox("选择签退记录查看整理照片", records, format_func=lambda item: f"预约 #{item['id']} · {item['band_name']} · {item['booking_date']}")
        photo_path = safe_photo_path(selected["checkout_photo_filename"])
        if photo_path:
            st.image(str(photo_path), caption=f"预约 #{selected['id']} 的签退整理照片")
