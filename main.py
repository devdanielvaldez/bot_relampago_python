from fastapi import FastAPI, HTTPException, Depends, status, Request, Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, Dict, List, Any
import sqlite3
import uuid
import requests
import json
import os
import secrets
import random
from datetime import datetime, time
import uvicorn

app = FastAPI(title="Relámpago Express API")

# Configuración de autenticación básica
security = HTTPBasic()
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "relampagoexpress"  # En producción usar variables de entorno

# URL del servicio de WhatsApp
WHATSAPP_SERVICE_URL = "http://localhost:3000"

DELIVERY_FEE = 100  # Costo fijo de envío por pedido

# Configuración de la base de datos
DATABASE_FILE = "relampago_express.db"

# Estados de la conversación
AWAITING_START = "awaiting_start"
AWAITING_MODE_SELECTION = "awaiting_mode_selection"

# Estados para pedidos de clientes (delivery)
AWAITING_CUSTOMER_NAME = "awaiting_customer_name"
AWAITING_ADDRESS = "awaiting_address"
AWAITING_PHONE = "awaiting_phone"
AWAITING_RESTAURANT = "awaiting_restaurant"
AWAITING_ORDER_DETAILS = "awaiting_order_details"
AWAITING_CONFIRMATION = "awaiting_confirmation"
AWAITING_MORE_ORDERS = "awaiting_more_orders"

# Estados para establecimientos (pickup)
AWAITING_BUSINESS_NAME = "awaiting_business_name"
AWAITING_PICKUP_ADDRESS = "awaiting_pickup_address"
AWAITING_BUSINESS_CONTACT = "awaiting_business_contact"
AWAITING_CUSTOMER_DELIVERY_ADDRESS = "awaiting_customer_delivery_address"
AWAITING_CUSTOMER_PHONE = "awaiting_customer_phone"
AWAITING_PACKAGE_DESCRIPTION = "awaiting_package_description"
AWAITING_PACKAGE_VALUE = "awaiting_package_value"
AWAITING_PICKUP_CONFIRMATION = "awaiting_pickup_confirmation"

# Estados de pedidos
STATUS_PENDING = "pending"  # Pendiente, sin asignar
STATUS_ASSIGNED = "assigned"  # Asignado a un repartidor pero no confirmado
STATUS_CONFIRMED = "confirmed"  # Confirmado por el repartidor
STATUS_PREPARING = "preparing"  # En preparación
STATUS_DELIVERING = "delivering"  # En camino para entrega
STATUS_COMPLETED = "completed"  # Completado
STATUS_REJECTED = "rejected"  # Rechazado
STATUS_CANCELLED = "cancelled"  # Cancelado

# Estructura para almacenar conversaciones en curso (temporal)
active_conversations = {}

# Lista de repartidores disponibles
DRIVERS = [
    {"code": "R-36", "name": "DRIVER R-36", "fullName": "Vairon Ramirez", "phone": "18098624984"},
    {"code": "R-09", "name": "DRIVER R-09", "fullName": "Robert Francisco", "phone": "18094966093"},
    {"code": "R-27", "name": "DRIVER R-27", "fullName": "Robert Jr.", "phone": "18097790282"},
    {"code": "R-45", "name": "DRIVER R-45", "fullName": "Genesis", "phone": "18498611022"},
    {"code": "R-54", "name": "DRIVER R-54", "fullName": "Jolizon", "phone": "18495277731"},
    {"code": "R-72", "name": "DRIVER R-72", "fullName": "Jose De La Cruz", "phone": "18096325833"},
]


# Definición de modelos de datos
class ChatRequest(BaseModel):
    message: str
    phone_number: str


class LocationData(BaseModel):
    phone_number: str
    latitude: float
    longitude: float


class OrderUpdate(BaseModel):
    status: str
    amount: Optional[float] = None
    notes: Optional[str] = None
    driver_code: Optional[str] = None


class DriverOrderAssignment(BaseModel):
    order_id: str
    driver_code: str


class DriverOrderCompleted(BaseModel):
    order_id: str
    driver_code: str


class DriverOrderAmount(BaseModel):
    order_id: str
    amount: float
    driver_code: str


# Inicializar base de datos
def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    # Tabla de usuarios (clientes y establecimientos)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        phone TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        type TEXT NOT NULL,  -- 'client' o 'business'
        address TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_order_date TEXT
    )
    ''')

    # Tabla de repartidores
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS drivers (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        active_orders INTEGER DEFAULT 0,
        total_orders INTEGER DEFAULT 0,
        available BOOLEAN DEFAULT 1
    )
    ''')

    # Tabla de pedidos con campos para coordenadas
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        customer_name TEXT,
        customer_phone TEXT NOT NULL,
        customer_address TEXT,
        latitude REAL,
        longitude REAL,
        address_type TEXT DEFAULT 'text',
        restaurant_name TEXT,
        restaurant_phone TEXT,
        restaurant_address TEXT,
        restaurant_latitude REAL,
        restaurant_longitude REAL,
        order_details TEXT,
        order_amount REAL DEFAULT 0,
        delivery_fee REAL DEFAULT 100,
        total_amount REAL DEFAULT 0,
        status TEXT DEFAULT 'pending',
        driver_code TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (driver_code) REFERENCES drivers (code)
    )
    ''')

    # Tabla de actualizaciones de estado
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS status_updates (
        update_id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL,
        status TEXT NOT NULL,
        notes TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (order_id) REFERENCES orders (order_id)
    )
    ''')

    # Tabla para conversaciones activas (persistencia)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS conversations (
        phone TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        data TEXT NOT NULL,  -- JSON serializado
        last_updated TEXT DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # Insertar o actualizar los repartidores predefinidos
    for driver in DRIVERS:
        cursor.execute('''
        INSERT OR REPLACE INTO drivers (code, name, phone, active_orders, total_orders, available)
        VALUES (?, ?, ?, 0, 0, 1)
        ''', (driver["code"], driver["name"], driver["phone"]))

    conn.commit()
    conn.close()


# Funciones de base de datos
def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def check_user_exists(phone_number):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users WHERE phone = ?", (phone_number,))
    user = cursor.fetchone()

    conn.close()
    return user if user else None


def create_or_update_user(phone, name, user_type, address=None):
    conn = get_db_connection()
    cursor = conn.cursor()

    # Verificar si el usuario ya existe
    cursor.execute("SELECT * FROM users WHERE phone = ?", (phone,))
    user = cursor.fetchone()

    if user:
        # Actualizar el usuario existente
        cursor.execute('''
        UPDATE users 
        SET name = ?, type = ?, address = ?, last_order_date = CURRENT_TIMESTAMP
        WHERE phone = ?
        ''', (name, user_type, address or user["address"], phone))
    else:
        # Crear nuevo usuario
        cursor.execute('''
        INSERT INTO users (phone, name, type, address, last_order_date)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (phone, name, user_type, address))

    conn.commit()
    conn.close()


def save_conversation_state(phone, state, data):
    """Guarda el estado de la conversación en la base de datos."""
    conn = get_db_connection()
    cursor = conn.cursor()

    json_data = json.dumps(data)

    cursor.execute('''
    INSERT OR REPLACE INTO conversations (phone, state, data, last_updated)
    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    ''', (phone, state, json_data))

    conn.commit()
    conn.close()


def load_conversation_state(phone):
    """Carga el estado de una conversación desde la base de datos."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM conversations WHERE phone = ?", (phone,))
    conversation = cursor.fetchone()

    conn.close()

    if conversation:
        try:
            data = json.loads(conversation["data"])
            return conversation["state"], data
        except:
            return None, None
    return None, None


def get_all_available_drivers():
    """Obtiene todos los repartidores disponibles para asignar un pedido."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Obtener todos los conductores disponibles con menos de 5 pedidos activos
    cursor.execute("SELECT * FROM drivers WHERE active_orders < 5 AND available = 1 ORDER BY active_orders ASC")
    available_drivers = cursor.fetchall()
    conn.close()

    return [dict(driver) for driver in available_drivers]


def broadcast_order_to_drivers(order_id):
    """Envía una notificación a todos los repartidores disponibles sobre un nuevo pedido."""
    drivers = get_all_available_drivers()
    if not drivers:
        return False

    # Obtener detalles del pedido
    order = get_order(order_id)
    if not order:
        return False

    success = False
    for driver in drivers:
        if order["type"] == "client_delivery":
            driver_message = f"""
*🚨 NUEVO PEDIDO DISPONIBLE #{order_id}*
------------------
Cliente: {order["customer_name"]}
Dirección: {order["customer_address"]}
Restaurant: {order["restaurant_name"]}

Detalles:
{order["order_details"]}
------------------
Para tomar este pedido, responde:
*#tomar {order_id}*
"""
        else:  # business_pickup
            driver_message = f"""
*🚨 NUEVO PICKUP DISPONIBLE #{order_id}*
------------------
Recoger en: {order["restaurant_name"]}
Dirección recogida: {order["restaurant_address"]}

Entregar a: {order["customer_address"]}
Descripción: {order["order_details"]}

Valor: ${order["order_amount"]}
Total: ${order["total_amount"]}
------------------
Para tomar este pedido, responde:
*#tomar {order_id}*
"""
        # Enviar mensaje al repartidor
        if forward_to_whatsapp(driver["phone"], driver_message, order_id):
            success = True

        # Si hay coordenadas, enviar también la ubicación
        if "latitude" in order and order["latitude"] and "longitude" in order and order["longitude"]:
            forward_location_to_driver(
                driver["phone"],
                order["latitude"],
                order["longitude"],
                order_id
            )

    return success


def assign_driver_to_order(order_id, driver_code):
    """Asigna un pedido a un repartidor y actualiza sus pedidos activos"""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Verificar si el pedido existe y no tiene un repartidor asignado o está disponible
    cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    order = cursor.fetchone()

    if not order:
        conn.close()
        return None

    # Si el pedido ya tiene un repartidor asignado y es el mismo, no hacemos nada
    if order["driver_code"] == driver_code and order["status"] != STATUS_PENDING:
        cursor.execute("SELECT * FROM drivers WHERE code = ?", (driver_code,))
        driver = cursor.fetchone()
        conn.close()
        return {"driver": dict(driver) if driver else None, "order": dict(order)}

    # Si el pedido tiene asignado otro repartidor, debemos cancelar esa asignación
    if order["driver_code"] and order["driver_code"] != driver_code:
        # Decrementar el contador del repartidor anterior
        cursor.execute('''
        UPDATE drivers 
        SET active_orders = active_orders - 1
        WHERE code = ?
        ''', (order["driver_code"],))

    # Actualizar el pedido con el código del repartidor
    cursor.execute("UPDATE orders SET driver_code = ?, status = ?, updated_at = CURRENT_TIMESTAMP WHERE order_id = ?",
                   (driver_code, STATUS_CONFIRMED, order_id))

    # Incrementar el contador de pedidos activos y totales del repartidor
    cursor.execute('''
    UPDATE drivers 
    SET active_orders = active_orders + 1, total_orders = total_orders + 1
    WHERE code = ?
    ''', (driver_code,))

    # Insertar actualización de estado
    cursor.execute('''
    INSERT INTO status_updates (update_id, order_id, status, notes)
    VALUES (?, ?, ?, ?)
    ''', (
        generate_short_uuid("upd"),
        order_id,
        STATUS_CONFIRMED,
        f"Pedido asignado al repartidor {driver_code}"
    ))

    conn.commit()

    # Obtener información actualizada del repartidor
    cursor.execute("SELECT * FROM drivers WHERE code = ?", (driver_code,))
    driver = cursor.fetchone()

    # Obtener información del pedido
    cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    updated_order = cursor.fetchone()

    conn.close()

    return {"driver": dict(driver) if driver else None, "order": dict(updated_order) if updated_order else None}


def mark_order_completed(order_id, driver_code):
    """Marca un pedido como completado y reduce los pedidos activos del repartidor"""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Verificar que el pedido exista y esté asignado al repartidor correcto
    cursor.execute("SELECT * FROM orders WHERE order_id = ? AND driver_code = ?",
                   (order_id, driver_code))
    order = cursor.fetchone()

    if not order:
        conn.close()
        return None

    # Actualizar el estado del pedido a completado
    cursor.execute("UPDATE orders SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE order_id = ?",
                   (STATUS_COMPLETED, order_id))

    # Insertar actualización de estado
    cursor.execute('''
    INSERT INTO status_updates (update_id, order_id, status, notes)
    VALUES (?, ?, ?, ?)
    ''', (
        generate_short_uuid("upd"),
        order_id,
        STATUS_COMPLETED,
        f"Pedido completado por repartidor {driver_code}"
    ))

    # Reducir el contador de pedidos activos del repartidor
    cursor.execute('''
    UPDATE drivers 
    SET active_orders = active_orders - 1
    WHERE code = ?
    ''', (driver_code,))

    conn.commit()

    # Obtener información actualizada del pedido
    cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    updated_order = cursor.fetchone()

    conn.close()

    if updated_order:
        return dict(updated_order)
    return None


def update_order_amount(order_id, amount, driver_code=None):
    """Actualiza el monto de un pedido"""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Verificar que el pedido exista
    query = "SELECT * FROM orders WHERE order_id = ?"
    params = [order_id]

    if driver_code:
        query += " AND driver_code = ?"
        params.append(driver_code)

    cursor.execute(query, params)
    order = cursor.fetchone()

    if not order:
        conn.close()
        return None

    # Actualizar el monto del pedido
    total_amount = amount + DELIVERY_FEE
    cursor.execute('''
    UPDATE orders 
    SET order_amount = ?, total_amount = ?, updated_at = CURRENT_TIMESTAMP 
    WHERE order_id = ?
    ''', (amount, total_amount, order_id))

    # Insertar actualización de estado
    cursor.execute('''
    INSERT INTO status_updates (update_id, order_id, status, notes)
    VALUES (?, ?, ?, ?)
    ''', (
        generate_short_uuid("upd"),
        order_id,
        order["status"],
        f"Monto actualizado a ${amount} (total ${total_amount} con cargo de servicio)"
    ))

    conn.commit()

    # Obtener información actualizada del pedido
    cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    updated_order = cursor.fetchone()

    conn.close()

    if updated_order:
        return dict(updated_order)
    return None


def insert_order(order_data):
    conn = get_db_connection()
    cursor = conn.cursor()

    # SQL query con soporte para campos de ubicación
    cursor.execute('''
    INSERT INTO orders 
    (order_id, type, customer_name, customer_phone, customer_address, 
    latitude, longitude, address_type,
    restaurant_name, restaurant_phone, restaurant_address,
    restaurant_latitude, restaurant_longitude,
    order_details, order_amount, delivery_fee, total_amount, status, driver_code)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        order_data["order_id"],
        order_data["type"],
        order_data.get("customer_name", ""),
        order_data["customer_phone"],
        order_data.get("customer_address", ""),
        order_data.get("latitude"),
        order_data.get("longitude"),
        order_data.get("address_type", "text"),
        order_data.get("restaurant_name", ""),
        order_data.get("restaurant_phone", ""),
        order_data.get("restaurant_address", ""),
        order_data.get("restaurant_latitude"),
        order_data.get("restaurant_longitude"),
        order_data.get("order_details", ""),
        order_data.get("order_amount", 0),
        order_data.get("delivery_fee", DELIVERY_FEE),
        order_data.get("total_amount", 0),
        order_data.get("status", STATUS_PENDING),
        order_data.get("driver_code", None)
    ))

    conn.commit()
    conn.close()


def update_order_status(order_id, status, notes=None, amount=None, driver_code=None):
    conn = get_db_connection()
    cursor = conn.cursor()

    # Verificar que el pedido exista
    cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    order_check = cursor.fetchone()

    if not order_check:
        conn.close()
        return None

    # Actualizar pedido
    update_query = "UPDATE orders SET status = ?, updated_at = CURRENT_TIMESTAMP"
    params = [status]

    if amount is not None:
        update_query += ", order_amount = ?, total_amount = ?"
        params.extend([amount, amount + DELIVERY_FEE])

    if driver_code is not None:
        update_query += ", driver_code = ?"
        params.append(driver_code)

    update_query += " WHERE order_id = ?"
    params.append(order_id)

    cursor.execute(update_query, params)

    # Insertar actualización de estado
    cursor.execute('''
    INSERT INTO status_updates (update_id, order_id, status, notes)
    VALUES (?, ?, ?, ?)
    ''', (
        generate_short_uuid("upd"),
        order_id,
        status,
        notes or ""
    ))

    conn.commit()

    # Obtener información del pedido para notificaciones
    cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    order = cursor.fetchone()

    conn.close()

    if order:
        return dict(order)
    return None


def get_order(order_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    order = cursor.fetchone()

    if order:
        # Obtener historial de actualizaciones
        cursor.execute("SELECT * FROM status_updates WHERE order_id = ? ORDER BY created_at", (order_id,))
        updates = cursor.fetchall()

        # Obtener información del repartidor asignado
        driver_info = None
        if order["driver_code"]:
            cursor.execute("SELECT * FROM drivers WHERE code = ?", (order["driver_code"],))
            driver = cursor.fetchone()
            if driver:
                driver_info = dict(driver)

        order_dict = dict(order)
        order_dict["updates"] = [dict(update) for update in updates]
        order_dict["driver"] = driver_info

        conn.close()
        return order_dict

    conn.close()
    return None


def get_all_orders(status=None, limit=100, offset=0):
    conn = get_db_connection()
    cursor = conn.cursor()

    query = "SELECT * FROM orders"
    params = []

    if status:
        query += " WHERE status = ?"
        params.append(status)

    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor.execute(query, params)
    orders = cursor.fetchall()

    conn.close()
    return [dict(order) for order in orders]


def get_driver_orders(driver_code):
    conn = get_db_connection()
    cursor = conn.cursor()

    # Obtener pedidos activos del repartidor
    cursor.execute('''
    SELECT * FROM orders 
    WHERE driver_code = ? AND status NOT IN (?, ?)
    ORDER BY created_at DESC
    ''', (driver_code, STATUS_COMPLETED, STATUS_CANCELLED))

    orders = cursor.fetchall()

    conn.close()
    return [dict(order) for order in orders]


def get_pending_orders():
    """Obtiene todos los pedidos pendientes sin asignar a un repartidor."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''
    SELECT * FROM orders 
    WHERE status = ? AND (driver_code IS NULL OR driver_code = '')
    ORDER BY created_at ASC
    ''', (STATUS_PENDING,))

    orders = cursor.fetchall()
    conn.close()

    return [dict(order) for order in orders]


def forward_to_whatsapp(to_number: str, message: str, order_id: str = None):
    try:
        whatsapp_url = f"{WHATSAPP_SERVICE_URL}/forward-message"
        payload = {
            "to": to_number,
            "message": message
        }
        if order_id:
            payload["order_id"] = order_id

        response = requests.post(whatsapp_url, json=payload, timeout=5)
        return response.status_code == 200
    except Exception as e:
        print(f"Error al enviar mensaje a WhatsApp: {e}")
        return False


def forward_location_to_driver(to_number: str, latitude: float, longitude: float, order_id: str = None):
    """Envía una ubicación a través de WhatsApp"""
    try:
        whatsapp_url = f"{WHATSAPP_SERVICE_URL}/forward-location"
        payload = {
            "to": to_number,
            "latitude": latitude,
            "longitude": longitude
        }
        if order_id:
            payload["order_id"] = order_id

        response = requests.post(whatsapp_url, json=payload, timeout=5)
        return response.status_code == 200
    except Exception as e:
        print(f"Error al enviar ubicación a WhatsApp: {e}")
        return False


# Verificación de credenciales
def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)

    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def generate_short_uuid(prefix="order"):
    """Genera un ID corto y único con un prefijo."""
    return f"{prefix}_{uuid.uuid4().hex[:6]}"


def is_valid_phone_number(phone):
    """Validación básica de números de teléfono."""
    # Eliminar caracteres no numéricos
    phone = ''.join(c for c in phone if c.isdigit())
    # Verificar longitud (8-15 dígitos es razonable para números internacionales)
    return 8 <= len(phone) <= 15


def check_if_driver(phone_number):
    """Verifica si un número de teléfono pertenece a un repartidor registrado."""
    for driver in DRIVERS:
        if driver["phone"] == phone_number:
            return driver
    return None


# Endpoint para recibir ubicaciones
@app.post("/location")
async def receive_location(data: LocationData):
    phone = data.phone_number

    # Si la conversación existe, actualizar con la ubicación
    if phone in active_conversations:
        conversation = active_conversations[phone]
        state = conversation["state"]

        # Guardar coordenadas en los datos de la conversación
        conversation["data"]["latitude"] = data.latitude
        conversation["data"]["longitude"] = data.longitude

        # Guardar estado en la base de datos para persistencia
        save_conversation_state(phone, conversation["state"], conversation["data"])

        # Actualizar la dirección dependiendo del estado actual
        if state == AWAITING_ADDRESS:
            conversation["data"]["customer_address_type"] = "location"

            # Si ya tenemos datos del usuario, intentar saltar el paso siguiente
            if "user" in conversation["data"] and is_valid_phone_number(
                    conversation["data"].get("user", {}).get("phone", "")):
                conversation["data"]["customer_phone"] = conversation["data"]["user"]["phone"]
                conversation["state"] = AWAITING_RESTAURANT

                # También guardar una referencia textual por si acaso
                conversation["data"]["customer_address"] = f"Ubicación compartida: {data.latitude}, {data.longitude}"

                save_conversation_state(phone, conversation["state"], conversation["data"])

                return {
                    "answer": "📍 He recibido tu ubicación. ¿En qué restaurante o establecimiento deseas hacer tu pedido?"
                }
            else:
                conversation["state"] = AWAITING_PHONE

                # También guardar una referencia textual por si acaso
                conversation["data"]["customer_address"] = f"Ubicación compartida: {data.latitude}, {data.longitude}"

                save_conversation_state(phone, conversation["state"], conversation["data"])

                return {
                    "answer": "📍 He recibido tu ubicación. Ahora necesito tu número de teléfono para contactarte si es necesario."
                }
        elif state == AWAITING_CUSTOMER_DELIVERY_ADDRESS:
            conversation["data"]["customer_address_type"] = "location"
            conversation["state"] = AWAITING_CUSTOMER_PHONE

            # También guardar una referencia textual por si acaso
            conversation["data"]["customer_address"] = f"Ubicación compartida: {data.latitude}, {data.longitude}"

            save_conversation_state(phone, conversation["state"], conversation["data"])

            return {
                "answer": "📍 He recibido la ubicación de entrega. Necesito el número de teléfono del cliente que recibirá el pedido."
            }
        elif state == AWAITING_PICKUP_ADDRESS:
            conversation["data"]["pickup_address_type"] = "location"
            conversation["state"] = AWAITING_CUSTOMER_DELIVERY_ADDRESS

            # También guardar una referencia textual por si acaso
            conversation["data"]["pickup_address"] = f"Ubicación compartida: {data.latitude}, {data.longitude}"
            conversation["data"]["restaurant_latitude"] = data.latitude
            conversation["data"]["restaurant_longitude"] = data.longitude

            save_conversation_state(phone, conversation["state"], conversation["data"])

            return {
                "answer": "📍 He recibido tu ubicación para recoger el pedido. Ahora necesito la dirección donde debemos entregar el paquete. Puedes escribirla o compartir otra ubicación."
            }

    # Si no hay conversación activa o no estamos esperando una dirección
    return {
        "answer": "📍 He recibido tu ubicación, pero no estamos en el paso adecuado del proceso. Por favor, sigue las instrucciones previas."
    }


# Rutas de chat para WhatsApp
@app.post("/chat")
async def chat(request: ChatRequest):
    phone = request.phone_number
    message = request.message.strip().lower()

    # Verificar si el usuario quiere reiniciar el flujo
    if message in ["reiniciar", "restart", "reset", "comenzar", "empezar", "iniciar"]:
        active_conversations[phone] = {
            "state": AWAITING_START,
            "data": {},
            "orders": []
        }
        save_conversation_state(phone, AWAITING_START, {})

        return {
            "answer": "🔄 *Proceso reiniciado*. ¡Bienvenido a *Relámpago Express*! ¿Cómo puedo ayudarte hoy?\n\n1️⃣ Escribe *\"1\"* o *\"Cliente\"* si deseas solicitar un envío\n2️⃣ Escribe *\"2\"* o *\"Establecimiento\"* si necesitas que recojamos algo para enviar"
        }

    # === COMANDOS PARA REPARTIDORES ===
    driver = check_if_driver(phone)

    # Comando para aceptar un pedido
    if message.startswith("#tomar ") and driver:
        order_id = message.replace("#tomar ", "").strip()
        result = assign_driver_to_order(order_id, driver["code"])

        if not result or not result.get("order"):
            return {
                "answer": f"❌ No se pudo asignar el pedido {order_id}. Posiblemente ya fue tomado por otro repartidor o no existe."
            }

        order = result["order"]

        # Notificar al cliente o establecimiento que su pedido fue tomado
        if order["type"] == "client_delivery":
            customer_message = f"""
*🛵 PEDIDO CONFIRMADO - RELÁMPAGO EXPRESS*
------------------
¡Buenas noticias! Tu pedido #{order_id} ha sido tomado por {driver["name"]}.
El repartidor se dirigirá a recoger tu pedido en {order["restaurant_name"]}.

Te notificaremos cuando el repartidor esté en camino a tu dirección.
------------------
"""
            forward_to_whatsapp(order["customer_phone"], customer_message)
        else:  # business_pickup
            business_message = f"""
*🛵 SERVICIO CONFIRMADO - RELÁMPAGO EXPRESS*
------------------
¡Buenas noticias! Tu solicitud de recogida #{order_id} ha sido tomada por {driver["name"]}.
El repartidor se dirigirá pronto a {order["restaurant_address"]} para recoger el paquete.

Te notificaremos cuando el pedido sea entregado a su destino.
------------------
"""
            forward_to_whatsapp(order["restaurant_phone"], business_message)

        return {
            "answer": f"""
✅ *¡Has tomado el pedido #{order_id}!*
------------------
Detalles del pedido:
• Tipo: {order["type"]}
• Cliente/Establecimiento: {order["type"] == "client_delivery" and order["customer_name"] or order["restaurant_name"]}
• {order["type"] == "client_delivery" and f"Restaurant: {order['restaurant_name']}" or f"Cliente destino: {order['customer_address']}"}

Acciones disponibles:
• Para actualizar precio: $ID MONTO o #p ID MONTO
• Para marcar entregado: #c ID
• Para ver tus pedidos: #pedidos
"""
        }

    # Comandos para actualizar precio, completar y ver pedidos
    if (message.startswith("#p ") or message.startswith("#c ") or message.startswith("#m ") or
        message.startswith("$") or message.startswith("#tomar")) and driver:

        # Extraer el comando y los parámetros
        if message.startswith("$"):
            # Formato simplificado: $ID MONTO
            parts = message[1:].split()
            command_type = "#p"  # Por defecto es precio
        elif message.startswith("#tomar"):
            parts = message.split(" ")
            command_type = "#tomar"
        else:
            parts = message.split(" ")
            command_type = parts[0].lower()

        if len(parts) >= 2 or (command_type == "#tomar" and len(parts) >= 2):
            try:
                order_id = parts[0] if message.startswith("$") else parts[1]
                # Limpiar posibles caracteres adicionales
                order_id = order_id.strip()

                # Para comandos de precio necesitamos el monto
                if command_type in ["#p", "#m", "$"] and len(parts) >= (2 if message.startswith("$") else 3):
                    amount_text = parts[1] if message.startswith("$") else parts[2]
                    amount = float(amount_text.replace("$", "").strip())

                    # Actualizar monto del pedido
                    updated_order = update_order_amount(order_id, amount, driver["code"])

                    if not updated_order:
                        return {
                            "answer": f"❌ No se encontró el pedido {order_id} o no está asignado a ti."
                        }

                    # Notificar al cliente el monto actualizado
                    if updated_order["type"] == "client_delivery":
                        customer_message = f"""
*💲 ACTUALIZACIÓN DE PRECIO - RELÁMPAGO EXPRESS*
------------------
Pedido: #{order_id}
Restaurant: {updated_order['restaurant_name']}

Monto del pedido: ${updated_order['order_amount']}
Cargo por servicio: ${updated_order['delivery_fee']}
Total a pagar: ${updated_order['total_amount']}
------------------
Tu pedido está en camino. Gracias por tu paciencia.
"""
                        forward_to_whatsapp(updated_order['customer_phone'], customer_message)

                    return {
                        "answer": f"✅ Monto del pedido {order_id} actualizado a ${amount}. El cliente ha sido notificado."
                    }

                # Para comandos de completar/entregar
                elif command_type in ["#co", "#en", "#c"]:
                    # Marcar el pedido como completado
                    completed_order = mark_order_completed(order_id, driver["code"])

                    if not completed_order:
                        return {
                            "answer": f"❌ No se encontró el pedido {order_id} o no está asignado a ti."
                        }

                    # Notificar al cliente que su pedido fue entregado
                    if completed_order["type"] == "client_delivery":
                        customer_message = f"""
*✅ PEDIDO ENTREGADO - RELÁMPAGO EXPRESS*
------------------
¡Tu pedido #{order_id} ha sido entregado!

Gracias por confiar en Relámpago Express.
Esperamos volver a servirte pronto.
------------------
"""
                        forward_to_whatsapp(completed_order['customer_phone'], customer_message)

                    # Si es pedido de establecimiento, notificar al establecimiento
                    elif completed_order["type"] == "business_pickup":
                        business_message = f"""
*✅ PEDIDO ENTREGADO - RELÁMPAGO EXPRESS*
------------------
¡El pedido #{order_id} ha sido entregado a su destino!

Gracias por confiar en Relámpago Express.
Esperamos volver a servirte pronto.
------------------
"""
                        forward_to_whatsapp(completed_order['restaurant_phone'], business_message)

                    return {
                        "answer": f"✅ Pedido {order_id} marcado como entregado. ¡Buen trabajo!"
                    }
                else:
                    return {
                        "answer": "⚠️ Comando no reconocido. Formatos válidos:\n- Actualizar precio: #p ID MONTO o $ID MONTO\n- Marcar entregado: #c ID\n- Tomar pedido: #tomar ID"
                    }

            except ValueError:
                return {
                    "answer": "⚠️ Formato incorrecto. Usa:\n- Actualizar precio: #p ID MONTO o $ID MONTO\n- Marcar entregado: #c ID\n- Tomar pedido: #tomar ID"
                }
        else:
            return {
                "answer": "⚠️ Faltan parámetros. Usa:\n- Actualizar precio: #p ID MONTO o $ID MONTO\n- Marcar entregado: #c ID\n- Tomar pedido: #tomar ID"
            }

    # Ver pedidos asignados para un repartidor
    if (message in ["#mispedidos", "#pedidos", "#p"]) and driver:
        # Obtener pedidos activos del repartidor
        orders = get_driver_orders(driver["code"])

        if not orders:
            # Si no hay pedidos activos, mostrar pedidos pendientes disponibles
            pending = get_pending_orders()

            if not pending:
                return {
                    "answer": "📋 No tienes pedidos activos en este momento y no hay pedidos pendientes disponibles."
                }

            # Mostrar lista de pedidos pendientes
            pending_list = ""
            for order in pending[:5]:  # Limitamos a 5 para no saturar el mensaje
                pending_list += f"""
• *#{order['order_id']}* - {order['type']}
  {order['type'] == 'client_delivery' and 'Cliente' or 'Establecimiento'}: {order['type'] == 'client_delivery' and order['customer_name'] or order['restaurant_name']}
  {order['type'] == 'client_delivery' and f"Restaurant: {order['restaurant_name']}" or f"Entrega a: {order['customer_address'][:30]}..."}
"""

            message = f"""
*📋 NO TIENES PEDIDOS ACTIVOS*
------------------
Pero hay {len(pending)} pedidos pendientes disponibles:
{pending_list}
------------------
Para tomar un pedido: #tomar ID
"""
            return {
                "answer": message
            }

        # Mostrar lista de pedidos de forma concisa
        orders_list = ""
        for order in orders:
            orders_list += f"""
• *#{order['order_id']}* - {order['status']}
  {order['type'] == 'client_delivery' and 'Cliente' or 'Establecimiento'}: {order['type'] == 'client_delivery' and (order['customer_name'] or 'Sin nombre') or order['restaurant_name']}
  {order['type'] == 'client_delivery' and f"Restaurant: {order['restaurant_name']}" or f"Dirección: {order['customer_address'][:30]}..."}
"""

        # Obtener pedidos pendientes disponibles
        pending = get_pending_orders()
        pending_info = f"\nHay {len(pending)} pedidos pendientes disponibles. Usa #tomar ID para aceptarlos." if pending else ""

        message = f"""
*📋 TUS PEDIDOS ACTIVOS*
{orders_list}
------------------
Acciones:
• Para actualizar precio: $ID MONTO
• Para marcar entregado: #c ID
{pending_info}
"""
        return {
            "answer": message
        }

    # Ver todos los pedidos pendientes para un repartidor
    if message in ["#pendientes", "#disponibles"] and driver:
        pending = get_pending_orders()

        if not pending:
            return {
                "answer": "📋 No hay pedidos pendientes disponibles en este momento."
            }

        # Mostrar lista de pedidos pendientes
        pending_list = ""
        for order in pending[:8]:  # Limitamos a 8 para no saturar el mensaje
            pending_list += f"""
• *#{order['order_id']}* - {order['type']}
  {order['type'] == 'client_delivery' and 'Cliente' or 'Establecimiento'}: {order['type'] == 'client_delivery' and order['customer_name'] or order['restaurant_name']}
  {order['type'] == 'client_delivery' and f"Restaurant: {order['restaurant_name']}" or f"Entrega a: {order['customer_address'][:30]}..."}
"""

        message = f"""
*📋 PEDIDOS DISPONIBLES ({len(pending)})*
{pending_list}
------------------
Para tomar un pedido: #tomar ID
"""
        return {
            "answer": message
        }

    # === FLUJO NORMAL DE CONVERSACIÓN ===
    # Inicializar conversación si no existe o cargar desde DB
    if phone not in active_conversations:
        # Intentar cargar desde la base de datos
        state, data = load_conversation_state(phone)

        if state:
            active_conversations[phone] = {
                "state": state,
                "data": data,
                "orders": data.get("orders", [])
            }
        else:
            active_conversations[phone] = {
                "state": AWAITING_START,
                "data": {},
                "orders": []
            }

            # Verificar si es un usuario recurrente
            existing_user = check_user_exists(phone)
            if existing_user:
                # Si el usuario ya existe, saludarlo por su nombre
                active_conversations[phone]["data"]["user"] = dict(existing_user)
                save_conversation_state(phone, AWAITING_START, active_conversations[phone]["data"])
                return {
                    "answer": f"""
🚚 *¡Bienvenido de nuevo a Relámpago Express, {existing_user['name']}!* 🚚

¿En qué podemos ayudarte hoy?

1️⃣ Escribe *"1"* o *"Cliente"* para pedir un envío
2️⃣ Escribe *"2"* o *"Establecimiento"* para solicitar una recogida
"""
                }

    conversation = active_conversations[phone]
    state = conversation["state"]

    # Estado inicial - mensaje de bienvenida y selección de modalidad
    if state == AWAITING_START:
        welcome_message = f"""
🚚 *¡Bienvenido a Relámpago Express!* 🚚

¿Cómo podemos ayudarte hoy?

1️⃣ Escribe *"1"* o *"Cliente"* para pedir un envío
2️⃣ Escribe *"2"* o *"Establecimiento"* para solicitar una recogida
"""
        conversation["state"] = AWAITING_MODE_SELECTION
        save_conversation_state(phone, AWAITING_MODE_SELECTION, conversation["data"])
        return {"answer": welcome_message}

    # Selección de modalidad
    elif state == AWAITING_MODE_SELECTION:
        if "1" == message or "cliente" in message or "pedido" in message or "envio" in message or "delivery" in message:
            # Cliente quiere pedir un envío
            conversation["data"]["mode"] = "cliente"

            # Si ya tenemos datos del usuario, saltamos algunos pasos
            if "user" in conversation["data"] and conversation["data"]["user"]["type"] == "client":
                conversation["data"]["customer_name"] = conversation["data"]["user"]["name"]
                conversation["data"]["customer_phone"] = phone  # Usar número actual
                conversation["data"]["customer_address"] = conversation["data"]["user"]["address"] or ""

                if conversation["data"]["user"]["address"]:
                    conversation["state"] = AWAITING_RESTAURANT
                    save_conversation_state(phone, AWAITING_RESTAURANT, conversation["data"])
                    return {
                        "answer": f"👋 Hola {conversation['data']['user']['name']}. Entregaremos en tu dirección habitual:\n{conversation['data']['user']['address']}\n\nPor favor dime de qué restaurante o establecimiento deseas ordenar."
                    }
                else:
                    conversation["state"] = AWAITING_ADDRESS
                    save_conversation_state(phone, AWAITING_ADDRESS, conversation["data"])
                    return {
                        "answer": f"👋 Hola {conversation['data']['user']['name']}. Por favor, indica la dirección exacta donde debemos entregar tu pedido o *comparte tu ubicación* 📍"
                    }
            else:
                conversation["state"] = AWAITING_CUSTOMER_NAME
                save_conversation_state(phone, AWAITING_CUSTOMER_NAME, conversation["data"])
                return {
                    "answer": "📝 Por favor, dime tu nombre completo."
                }

        elif "2" == message or "establecimiento" in message or "negocio" in message or "recoger" in message or "pickup" in message:
            # Establecimiento quiere que recojamos un pedido
            conversation["data"]["mode"] = "establecimiento"

            # Si ya tenemos datos del usuario, saltamos algunos pasos
            if "user" in conversation["data"] and conversation["data"]["user"]["type"] == "business":
                conversation["data"]["business_name"] = conversation["data"]["user"]["name"]
                conversation["data"]["business_phone"] = phone  # Usar número actual
                conversation["data"]["pickup_address"] = conversation["data"]["user"]["address"] or ""

                if conversation["data"]["user"]["address"]:
                    conversation["state"] = AWAITING_CUSTOMER_DELIVERY_ADDRESS
                    save_conversation_state(phone, AWAITING_CUSTOMER_DELIVERY_ADDRESS, conversation["data"])
                    return {
                        "answer": f"👋 Hola {conversation['data']['user']['name']}. Recogeremos en tu dirección habitual:\n{conversation['data']['user']['address']}\n\nPor favor, indica la dirección exacta donde debemos entregar el pedido o *comparte la ubicación de entrega* 📍"
                    }
                else:
                    conversation["state"] = AWAITING_PICKUP_ADDRESS
                    save_conversation_state(phone, AWAITING_PICKUP_ADDRESS, conversation["data"])
                    return {
                        "answer": f"👋 Hola {conversation['data']['user']['name']}. Por favor, indica la dirección exacta donde debemos recoger el pedido o *comparte tu ubicación* 📍"
                    }
            else:
                conversation["state"] = AWAITING_BUSINESS_NAME
                save_conversation_state(phone, AWAITING_BUSINESS_NAME, conversation["data"])
                return {
                    "answer": "📝 Por favor, dime el nombre de tu negocio o establecimiento."
                }
        else:
            return {
                "answer": "❓ Por favor selecciona una opción válida:\n\n1️⃣ Escribe *\"1\"* o *\"Cliente\"* para pedir un envío\n2️⃣ Escribe *\"2\"* o *\"Establecimiento\"* para solicitar una recogida"
            }

    # ======= FLUJO PARA CLIENTES (DELIVERY) =======
    elif state == AWAITING_CUSTOMER_NAME:
        conversation["data"]["customer_name"] = message
        conversation["data"]["customer_phone"] = phone
        conversation["state"] = AWAITING_ADDRESS
        save_conversation_state(phone, AWAITING_ADDRESS, conversation["data"])
        return {
            "answer": f"Gracias, {message.capitalize()}. Por favor, indica la dirección exacta donde debemos entregar tu pedido o *comparte tu ubicación* 📍"
        }

    elif state == AWAITING_ADDRESS:
        # Si el usuario respondió "sí", mantener la dirección anterior
        if message.lower() in ["si", "sí", "yes"]:
            if "user" in conversation["data"] and conversation["data"]["user"]["address"]:
                conversation["data"]["customer_address"] = conversation["data"]["user"]["address"]
                conversation["data"]["customer_address_type"] = "text"
            else:
                return {
                    "answer": "⚠️ No tenemos registrada una dirección anterior. Por favor, indica la dirección exacta donde debemos entregar tu pedido o *comparte tu ubicación* 📍"
                }
        else:
            conversation["data"]["customer_address"] = message
            conversation["data"]["customer_address_type"] = "text"

        # Si ya tenemos el teléfono, saltamos al siguiente paso
        if "customer_phone" in conversation["data"] and is_valid_phone_number(conversation["data"]["customer_phone"]):
            conversation["state"] = AWAITING_RESTAURANT
            save_conversation_state(phone, AWAITING_RESTAURANT, conversation["data"])
            return {
                "answer": "Perfecto. Ahora, indica el nombre del restaurante o establecimiento donde se recogerá el pedido."
            }
        else:
            conversation["state"] = AWAITING_PHONE
            save_conversation_state(phone, AWAITING_PHONE, conversation["data"])
            return {
                "answer": "Gracias. Ahora necesito tu número de teléfono para contactarte si es necesario."
            }

    elif state == AWAITING_PHONE:
        # Validación simple del número de teléfono
        if not is_valid_phone_number(message):
            return {
                "answer": "⚠️ Por favor, ingresa un número de teléfono válido (8-15 dígitos)."
            }

        conversation["data"]["customer_phone"] = message
        conversation["state"] = AWAITING_RESTAURANT
        save_conversation_state(phone, AWAITING_RESTAURANT, conversation["data"])
        return {
            "answer": "Excelente. Ahora, indica el nombre del restaurante o establecimiento donde se recogerá el pedido."
        }

    elif state == AWAITING_RESTAURANT:
        conversation["data"]["restaurant_name"] = message
        conversation["state"] = AWAITING_ORDER_DETAILS
        save_conversation_state(phone, AWAITING_ORDER_DETAILS, conversation["data"])
        return {
            "answer": "Gracias. Por favor, describe detalladamente lo que deseas ordenar (platos, cantidades, instrucciones especiales)."
        }

    elif state == AWAITING_ORDER_DETAILS:
        conversation["data"]["order_details"] = message

        # Mostrar resumen y pedir confirmación
        conversation["state"] = AWAITING_CONFIRMATION
        save_conversation_state(phone, AWAITING_CONFIRMATION, conversation["data"])

        summary = f"""
📋 *RESUMEN DE TU PEDIDO*
------------------
Cliente: {conversation["data"]["customer_name"]}
Dirección: {conversation["data"]["customer_address"]}
Teléfono: {conversation["data"]["customer_phone"]}
Restaurant: {conversation["data"]["restaurant_name"]}

Tu orden:
{conversation["data"]["order_details"]}
------------------
*Nota:* El monto se determinará al recoger tu pedido. 
Cargo por servicio: ${DELIVERY_FEE}

✅ Responde *"1"* o *"confirmar"* para procesar tu pedido
❌ Responde *"2"* o *"cancelar"* para descartar
"""

        return {"answer": summary}

    # ======= FLUJO PARA ESTABLECIMIENTOS (PICKUP) =======
    elif state == AWAITING_BUSINESS_NAME:
        conversation["data"]["business_name"] = message
        conversation["data"]["business_phone"] = phone
        conversation["state"] = AWAITING_PICKUP_ADDRESS
        save_conversation_state(phone, AWAITING_PICKUP_ADDRESS, conversation["data"])
        return {
            "answer": f"Gracias, {message}. Por favor, indica la dirección exacta donde debemos recoger el pedido o *comparte tu ubicación* 📍"
        }

    elif state == AWAITING_PICKUP_ADDRESS:
        # Si el usuario respondió "sí", mantener la dirección anterior
        if message.lower() in ["si", "sí", "yes"]:
            if "user" in conversation["data"] and conversation["data"]["user"]["address"]:
                conversation["data"]["pickup_address"] = conversation["data"]["user"]["address"]
                conversation["data"]["pickup_address_type"] = "text"
            else:
                return {
                    "answer": "⚠️ No tenemos registrada una dirección anterior. Por favor, indica la dirección exacta donde debemos recoger el pedido o *comparte tu ubicación* 📍"
                }
        else:
            conversation["data"]["pickup_address"] = message
            conversation["data"]["pickup_address_type"] = "text"

        # Si ya tenemos el contacto del negocio, podemos saltar al siguiente paso
        conversation["state"] = AWAITING_CUSTOMER_DELIVERY_ADDRESS
        save_conversation_state(phone, AWAITING_CUSTOMER_DELIVERY_ADDRESS, conversation["data"])
        return {
            "answer": "Perfecto. Ahora, indica la dirección exacta donde debemos entregar el pedido o *comparte la ubicación de entrega* 📍"
        }

    elif state == AWAITING_CUSTOMER_DELIVERY_ADDRESS:
        conversation["data"]["customer_address"] = message
        conversation["data"]["customer_address_type"] = "text"
        conversation["state"] = AWAITING_CUSTOMER_PHONE
        save_conversation_state(phone, AWAITING_CUSTOMER_PHONE, conversation["data"])
        return {
            "answer": "Gracias. Necesito el número de teléfono del cliente que recibirá el pedido."
        }

    elif state == AWAITING_CUSTOMER_PHONE:
        # Validación simple del número de teléfono
        if not is_valid_phone_number(message):
            return {
                "answer": "⚠️ Por favor, ingresa un número de teléfono válido para el cliente (8-15 dígitos)."
            }

        conversation["data"]["customer_phone"] = message
        conversation["state"] = AWAITING_PACKAGE_DESCRIPTION
        save_conversation_state(phone, AWAITING_PACKAGE_DESCRIPTION, conversation["data"])
        return {
            "answer": "Perfecto. Por favor, describe brevemente el pedido o paquete que vamos a transportar."
        }

    elif state == AWAITING_PACKAGE_DESCRIPTION:
        conversation["data"]["package_description"] = message
        conversation["state"] = AWAITING_PACKAGE_VALUE
        save_conversation_state(phone, AWAITING_PACKAGE_VALUE, conversation["data"])
        return {
            "answer": "Gracias. Por último, ¿cuál es el valor total del pedido? (solo el número)"
        }

    elif state == AWAITING_PACKAGE_VALUE:
        try:
            # Limpiar posibles símbolos de moneda
            amount_text = message.replace("$", "").replace("pesos", "").replace("RD", "").strip()
            package_value = float(amount_text)
            conversation["data"]["package_value"] = package_value

            # Calcular cargo por servicio
            delivery_fee = DELIVERY_FEE
            conversation["data"]["delivery_fee"] = delivery_fee
            conversation["data"]["total_amount"] = package_value + delivery_fee

            # Mostrar resumen y pedir confirmación
            conversation["state"] = AWAITING_PICKUP_CONFIRMATION
            save_conversation_state(phone, AWAITING_PICKUP_CONFIRMATION, conversation["data"])

            summary = f"""
📋 *RESUMEN DEL SERVICIO*
------------------
Establecimiento: {conversation["data"]["business_name"]}
Dirección de recogida: {conversation["data"]["pickup_address"]}
Contacto establecimiento: {conversation["data"]["business_phone"]}

Cliente: {conversation["data"]["customer_phone"]}
Dirección de entrega: {conversation["data"]["customer_address"]}
Descripción: {conversation["data"]["package_description"]}

Valor del pedido: ${package_value}
Cargo por servicio: ${delivery_fee}
Total: ${package_value + delivery_fee}
------------------
✅ Responde *"1"* o *"confirmar"* para solicitar el servicio
❌ Responde *"2"* o *"cancelar"* para descartar
"""
            return {"answer": summary}
        except ValueError:
            return {
                "answer": "⚠️ Por favor, ingresa solo el monto numérico (ej. 350)."
            }

    # ======= CONFIRMACIÓN DE PEDIDOS =======
    elif state in [AWAITING_CONFIRMATION, AWAITING_PICKUP_CONFIRMATION]:
        if message == "1" or "confirmar" in message or "sí" in message or "si" in message:
            # Generar ID único para el pedido
            order_id = generate_short_uuid("order")
            order_data = conversation["data"]

            # Guardar pedido en la lista de pedidos del usuario
            if "orders" not in conversation:
                conversation["orders"] = []
            conversation["orders"].append(order_id)
            conversation["data"]["orders"] = conversation["orders"]

            save_conversation_state(phone, conversation["state"], conversation["data"])

            # Formato del mensaje según el tipo de pedido
            if order_data.get("mode") == "cliente":
                # Guardar/actualizar información del cliente en la base de datos
                create_or_update_user(
                    phone=phone,
                    name=order_data["customer_name"],
                    user_type="client",
                    address=order_data["customer_address"]
                )

                # Datos del pedido cliente para la BD
                db_order = {
                    "order_id": order_id,
                    "type": "client_delivery",
                    "customer_name": order_data["customer_name"],
                    "customer_phone": phone,  # Usar el número real del cliente
                    "customer_address": order_data["customer_address"],
                    "address_type": order_data.get("customer_address_type", "text"),
                    "latitude": order_data.get("latitude"),
                    "longitude": order_data.get("longitude"),
                    "restaurant_name": order_data["restaurant_name"],
                    "restaurant_address": "",  # Se puede agregar más tarde
                    "restaurant_phone": "",  # Se puede agregar más tarde
                    "restaurant_latitude": None,
                    "restaurant_longitude": None,
                    "order_details": order_data["order_details"],
                    "order_amount": 0,  # Será definido por el repartidor
                    "delivery_fee": DELIVERY_FEE,
                    "total_amount": DELIVERY_FEE,  # Inicialmente solo el costo de envío
                    "status": STATUS_PENDING,
                    "driver_code": None  # No asignamos repartidor inicialmente
                }

                # Guardar en la base de datos
                insert_order(db_order)

                # Notificar a todos los repartidores disponibles
                broadcast_success = broadcast_order_to_drivers(order_id)

                # Preguntar si quiere hacer otro pedido
                conversation["state"] = AWAITING_MORE_ORDERS
                save_conversation_state(phone, AWAITING_MORE_ORDERS, conversation["data"])

                return {
                    "answer": f"""
✅ *¡Pedido confirmado!*
Tu número de pedido: *{order_id}*

Tu pedido ha sido notificado a nuestros repartidores disponibles.
Uno de ellos lo tomará y te notificaremos cuando lo haga.

¿Deseas hacer otro pedido?
1️⃣ Responde *"Sí"* para hacer otro pedido
2️⃣ Responde *"No"* para finalizar
"""
                }

            else:  # Modo establecimiento
                # Guardar/actualizar información del establecimiento en la base de datos
                create_or_update_user(
                    phone=phone,
                    name=order_data["business_name"],
                    user_type="business",
                    address=order_data["pickup_address"]
                )

                # Pedido desde establecimiento para la BD
                db_order = {
                    "order_id": order_id,
                    "type": "business_pickup",
                    "customer_name": "",  # Podría ser el destinatario
                    "customer_phone": order_data["customer_phone"],
                    "customer_address": order_data["customer_address"],
                    "address_type": order_data.get("customer_address_type", "text"),
                    "latitude": order_data.get("latitude"),
                    "longitude": order_data.get("longitude"),
                    "restaurant_name": order_data["business_name"],
                    "restaurant_address": order_data["pickup_address"],
                    "restaurant_phone": phone,  # Usar el número real del establecimiento
                    "restaurant_latitude": order_data.get("restaurant_latitude"),
                    "restaurant_longitude": order_data.get("restaurant_longitude"),
                    "order_details": order_data["package_description"],
                    "order_amount": order_data["package_value"],
                    "delivery_fee": DELIVERY_FEE,
                    "total_amount": order_data["package_value"] + DELIVERY_FEE,
                    "status": STATUS_PENDING,
                    "driver_code": None  # No asignamos repartidor inicialmente
                }

                # Guardar en la base de datos
                insert_order(db_order)

                # Notificar a todos los repartidores disponibles
                broadcast_success = broadcast_order_to_drivers(order_id)

                # Preguntar si quiere hacer otro pedido
                conversation["state"] = AWAITING_MORE_ORDERS
                save_conversation_state(phone, AWAITING_MORE_ORDERS, conversation["data"])

                return {
                    "answer": f"""
✅ *¡Servicio confirmado!*
Referencia: *{order_id}*

Tu solicitud ha sido notificada a nuestros repartidores disponibles.
Uno de ellos la tomará y pasará a recoger el pedido pronto.
Te notificaremos cuando sea asignado y cuando sea entregado.

¿Deseas solicitar otro servicio?
1️⃣ Responde *"Sí"* para solicitar otro servicio
2️⃣ Responde *"No"* para finalizar
"""
                }

        elif message == "2" or "cancelar" in message or "no" in message:
            # Volver al inicio pero mantener datos del usuario para futuras interacciones
            user_data = conversation["data"].get("user", None)
            conversation["state"] = AWAITING_START
            conversation["data"] = {}
            if user_data:
                conversation["data"]["user"] = user_data

            save_conversation_state(phone, AWAITING_START, conversation["data"])

            return {
                "answer": "❌ Has cancelado la solicitud. Si necesitas nuestros servicios más tarde, solo envía cualquier mensaje para comenzar."
            }
        else:
            return {
                "answer": "⚠️ Por favor, responde *'1'* o *'confirmar'* para procesar o *'2'* o *'cancelar'* para descartar."
            }

    # ======= MANEJO DE MÚLTIPLES PEDIDOS =======
    elif state == AWAITING_MORE_ORDERS:
        if message == "1" or any(keyword in message for keyword in ["si", "sí", "ok", "otro", "nueva"]):
            # Reiniciar datos pero mantener el modo y datos del usuario
            current_mode = conversation["data"].get("mode", "cliente")
            user_data = conversation["data"].get("user", None)
            orders = conversation["orders"]  # Preservar los IDs de los pedidos
            conversation["data"] = {"mode": current_mode, "orders": orders}
            if user_data:
                conversation["data"]["user"] = user_data

            if current_mode == "cliente":
                if user_data and user_data["type"] == "client":
                    conversation["data"]["customer_name"] = user_data["name"]
                    conversation["data"]["customer_phone"] = phone
                    conversation["state"] = AWAITING_ADDRESS
                    save_conversation_state(phone, AWAITING_ADDRESS, conversation["data"])
                    return {
                        "answer": f"📝 Iniciemos tu nuevo pedido. Por favor, indica la dirección de entrega o *comparte tu ubicación* 📍"
                    }
                else:
                    conversation["state"] = AWAITING_CUSTOMER_NAME
                    save_conversation_state(phone, AWAITING_CUSTOMER_NAME, conversation["data"])
                    return {
                        "answer": "📝 Iniciemos tu nuevo pedido. Por favor, dime tu nombre completo."
                    }
            else:
                if user_data and user_data["type"] == "business":
                    conversation["data"]["business_name"] = user_data["name"]
                    conversation["data"]["business_phone"] = phone
                    conversation["state"] = AWAITING_PICKUP_ADDRESS
                    save_conversation_state(phone, AWAITING_PICKUP_ADDRESS, conversation["data"])
                    return {
                        "answer": f"📝 Iniciemos tu nueva solicitud. Por favor, indica la dirección de recogida o *comparte tu ubicación* 📍"
                    }
                else:
                    conversation["state"] = AWAITING_BUSINESS_NAME
                    save_conversation_state(phone, AWAITING_BUSINESS_NAME, conversation["data"])
                    return {
                        "answer": "📝 Iniciemos tu nueva solicitud. Por favor, indica el nombre de tu establecimiento."
                    }
        else:
            # Finalizar proceso pero mantener datos del usuario para futuras interacciones
            user_data = conversation["data"].get("user", None)
            conversation["state"] = AWAITING_START
            conversation["data"] = {}
            if user_data:
                conversation["data"]["user"] = user_data

            save_conversation_state(phone, AWAITING_START, conversation["data"])

            return {
                "answer": """
👍 *¡Gracias por usar Relámpago Express!*

Estaremos en contacto para informarte sobre el estado de tu(s) pedido(s).
Si necesitas otro servicio, solo envía cualquier mensaje.

¡Que tengas un excelente día!
"""
            }

    return {
        "answer": "⚠️ Lo siento, no pude procesar tu solicitud. Por favor escribe *'reiniciar'* para comenzar de nuevo."}


# API para repartidores
@app.post("/api/driver/assign")
async def driver_assign_order(assignment: DriverOrderAssignment, username: str = Depends(verify_admin)):
    result = assign_driver_to_order(assignment.order_id, assignment.driver_code)

    if not result or not result.get("order"):
        raise HTTPException(status_code=400, detail="No se pudo asignar el pedido al repartidor")

    # Notificar al repartidor
    driver = result["driver"]
    order = result["order"]

    if order["type"] == "client_delivery":
        driver_message = f"""
*🛵 NUEVO PEDIDO #{order['order_id']}*
------------------
Cliente: {order["customer_name"]}
Teléfono: {order["customer_phone"]}
Dirección: {order["customer_address"]}
Restaurant: {order["restaurant_name"]}

Detalles:
{order["order_details"]}
------------------
*Acciones rápidas:*
• Actualizar precio: $ID MONTO
• Marcar entregado: #c ID
• Ver pedidos: #p
"""
    else:  # business_pickup
        driver_message = f"""
*🛵 NUEVO PICKUP #{order['order_id']}*
------------------
Recoger en: {order["restaurant_name"]}
Dirección recogida: {order["restaurant_address"]}
Contacto: {order["restaurant_phone"]}

Entregar a: {order["customer_address"]}
Contacto cliente: {order["customer_phone"]}
Descripción: {order["order_details"]}

Valor: ${order["order_amount"]}
Total: ${order["total_amount"]}
------------------
*Acciones rápidas:*
• Marcar entregado: #c {order['order_id']}
• Ver pedidos: #p
"""

    # Enviar mensaje al repartidor
    forward_to_whatsapp(driver["phone"], driver_message, order["order_id"])

    # Si tenemos coordenadas, enviar la ubicación real
    if order.get("latitude") and order.get("longitude"):
        forward_location_to_driver(
            driver["phone"],
            order["latitude"],
            order["longitude"],
            order["order_id"]
        )

    # Notificar al cliente o establecimiento
    customer_message = f"""
*🛵 REPARTIDOR ASIGNADO - RELÁMPAGO EXPRESS*
------------------
Tu {order['type'] == 'client_delivery' and 'pedido' or 'solicitud'} #{order['order_id']} 
ha sido asignada a {driver['name']}.

Pronto recibirás actualizaciones sobre el estado de tu envío.
------------------
"""

    if order['type'] == 'client_delivery':
        forward_to_whatsapp(order['customer_phone'], customer_message)
    else:  # business_pickup
        forward_to_whatsapp(order['restaurant_phone'], customer_message)

    return {
        "status": "success",
        "message": f"Pedido {order['order_id']} asignado al repartidor {driver['code']}",
        "order": order,
        "driver": driver
    }


@app.post("/api/driver/complete")
async def driver_complete_order(data: DriverOrderCompleted):
    order = mark_order_completed(data.order_id, data.driver_code)

    if not order:
        raise HTTPException(status_code=400, detail="No se pudo completar el pedido")

    # Notificar al cliente
    customer_message = f"""
*✅ PEDIDO ENTREGADO - RELÁMPAGO EXPRESS*
------------------
¡Tu pedido #{data.order_id} ha sido entregado!

Gracias por confiar en Relámpago Express.
Esperamos volver a servirte pronto.
------------------
"""

    if order["type"] == "client_delivery":
        forward_to_whatsapp(order["customer_phone"], customer_message)
    else:
        # Notificar también al establecimiento
        business_message = f"""
*✅ PEDIDO ENTREGADO - RELÁMPAGO EXPRESS*
------------------
¡El pedido #{data.order_id} ha sido entregado a su destino!

Gracias por confiar en Relámpago Express.
Esperamos volver a servirte pronto.
------------------
"""
        forward_to_whatsapp(order["restaurant_phone"], business_message)

    return {
        "status": "success",
        "message": f"Pedido {data.order_id} completado por repartidor {data.driver_code}",
        "order": order
    }


@app.post("/api/driver/update-amount")
async def driver_update_amount(data: DriverOrderAmount):
    order = update_order_amount(data.order_id, data.amount, data.driver_code)

    if not order:
        raise HTTPException(status_code=400, detail="No se pudo actualizar el monto del pedido")

    # Notificar al cliente
    if order["type"] == "client_delivery":
        customer_message = f"""
*💲 ACTUALIZACIÓN DE PRECIO - RELÁMPAGO EXPRESS*
------------------
Pedido: #{data.order_id}
Restaurant: {order['restaurant_name']}

Monto del pedido: ${order['order_amount']}
Cargo por servicio: ${order['delivery_fee']}
Total a pagar: ${order['total_amount']}
------------------
Tu pedido está en camino. Gracias por tu paciencia.
"""
        forward_to_whatsapp(order["customer_phone"], customer_message)

    return {
        "status": "success",
        "message": f"Monto del pedido {data.order_id} actualizado a ${data.amount}",
        "order": order
    }


@app.get("/api/drivers")
async def get_drivers_api(username: str = Depends(verify_admin)):
    """Obtiene lista de todos los repartidores"""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM drivers ORDER BY active_orders ASC, code")
    drivers = cursor.fetchall()

    conn.close()

    return {"drivers": [dict(driver) for driver in drivers]}


@app.post("/restaurant-response/{order_id}")
async def restaurant_response(order_id: str, action: dict):
    """
    Endpoint para confirmar o rechazar un pedido.
    El parámetro 'action' debe ser 'confirm' o 'reject'.
    """
    order = get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido no encontrado")

    action_value = action.get('action')

    if action_value == "confirm":
        # Actualizar estado en la BD
        updated_order = update_order_status(
            order_id,
            STATUS_CONFIRMED,
            notes="Confirmado por sistema"
        )

        if not updated_order:
            raise HTTPException(status_code=400, detail="Error al actualizar el pedido")

        order_type = updated_order.get("type", "client_delivery")

        if order_type == "client_delivery":
            # Mensaje para el cliente en caso de pedido normal
            customer_message = f"""
*🚚 PEDIDO CONFIRMADO - RELÁMPAGO EXPRESS*
------------------
¡Buenas noticias! Tu pedido #{order_id} ha sido confirmado.
Un repartidor ya va en camino a recoger tu pedido en {updated_order['restaurant_name']}.

{'' if updated_order['order_amount'] == 0 else f"Monto del pedido: ${updated_order['order_amount']}"}
Cargo servicio: ${DELIVERY_FEE}
{'' if updated_order['order_amount'] == 0 else f"Total a pagar: ${updated_order['total_amount']}"}
------------------
Tu pedido será entregado pronto en:
{updated_order['customer_address']}

¡Gracias por confiar en Relámpago Express!
"""
            # Notificar al cliente
            forward_to_whatsapp(updated_order['customer_phone'], customer_message)

        else:  # Tipo establecimiento
            # Mensaje para el establecimiento
            business_message = f"""
*🚚 SERVICIO CONFIRMADO - RELÁMPAGO EXPRESS*
------------------
¡Buenas noticias! Tu solicitud de recogida #{order_id} ha sido confirmada.
Un repartidor ya va en camino a {updated_order['restaurant_address']}.

Descripción: {updated_order['order_details']}
Valor: ${updated_order['order_amount']}
Cargo servicio: ${updated_order['delivery_fee']}
Total: ${updated_order['total_amount']}
------------------
El pedido será entregado pronto a tu cliente en:
{updated_order['customer_address']}

¡Gracias por confiar en Relámpago Express!
"""
            # Notificar al establecimiento
            forward_to_whatsapp(updated_order['restaurant_phone'], business_message)

        return {
            "status": "success",
            "message": "Pedido confirmado y notificaciones enviadas"
        }

    elif action_value == "reject":
        # Actualizar estado en la BD
        updated_order = update_order_status(
            order_id,
            STATUS_REJECTED,
            notes="Rechazado por sistema"
        )

        if not updated_order:
            raise HTTPException(status_code=400, detail="Error al actualizar el pedido")

        order_type = updated_order.get("type", "client_delivery")

        if order_type == "client_delivery":
            # Mensaje para el cliente en caso de pedido rechazado
            customer_message = f"""
*❌ PEDIDO NO DISPONIBLE - RELÁMPAGO EXPRESS*
------------------
Lo sentimos, tu pedido #{order_id} no pudo ser procesado en este momento.
Esto puede deberse a falta de disponibilidad en tu zona.

Por favor, intenta nuevamente más tarde.
Si necesitas ayuda, contáctanos al 829-920-3243.
"""
            # Notificar al cliente
            forward_to_whatsapp(updated_order['customer_phone'], customer_message)

        else:  # Tipo establecimiento
            # Mensaje para el establecimiento
            business_message = f"""
*❌ SERVICIO NO DISPONIBLE - RELÁMPAGO EXPRESS*
------------------
Lo sentimos, tu solicitud #{order_id} no pudo ser procesada en este momento.
Esto puede deberse a falta de disponibilidad en tu zona.

Por favor, intenta nuevamente más tarde.
Si necesitas ayuda, contáctanos al 829-920-3243.
"""
            # Notificar al establecimiento
            forward_to_whatsapp(updated_order['restaurant_phone'], business_message)

        return {
            "status": "success",
            "message": "Pedido rechazado y notificaciones enviadas"
        }

    else:
        raise HTTPException(status_code=400, detail="Acción no válida. Use 'confirm' o 'reject'")


# API endpoints para obtener datos
@app.get("/api/order/{order_id}")
async def get_order_api(order_id: str, username: str = Depends(verify_admin)):
    order = get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido no encontrado")
    return order


@app.get("/api/orders")
async def get_orders_api(status: str = None, limit: int = 200, offset: int = 0, username: str = Depends(verify_admin)):
    orders = get_all_orders(status, limit, offset)
    return {"orders": orders, "count": len(orders)}


@app.get("/api/users")
async def get_users_api(username: str = Depends(verify_admin)):
    """Obtiene todos los usuarios registrados en el sistema"""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users ORDER BY created_at DESC")
    users = cursor.fetchall()

    conn.close()

    return {"users": [dict(user) for user in users]}


# Función para notificar a los clientes cuando hay cambios en el pedido
def notify_customer(order):
    # Determinar el mensaje según el estado del pedido
    order_id = order["order_id"]
    status = order["status"]
    contact = order["customer_phone"]

    if order["type"] == "business_pickup":
        contact = order["restaurant_phone"]

    message = f"*🚚 ACTUALIZACIÓN DE PEDIDO #{order_id}*\n------------------\n"

    if status == STATUS_PREPARING:
        message += "Tu pedido está siendo preparado en el restaurante."
    elif status == STATUS_DELIVERING:
        message += "¡Tu pedido está en camino! Nuestro repartidor ya va hacia ti."
    elif status == STATUS_COMPLETED:
        message += "¡Tu pedido ha sido entregado! Gracias por confiar en Relámpago Express."
    elif status == STATUS_CANCELLED:
        message += "Tu pedido ha sido cancelado. Si tienes preguntas, contáctanos."
    else:
        message += f"Estado actualizado a: {status}"

    # Si se actualizó el monto (para pedidos de clientes)
    if order["type"] == "client_delivery" and order["order_amount"] > 0:
        message += f"""
------------------
Detalles de tu pedido:
{order["order_details"]}

Monto del pedido: ${order["order_amount"]}
Cargo por servicio: ${order["delivery_fee"]}
Total a pagar: ${order["total_amount"]}
"""

    message += "\n------------------\nRelámpago Express - Servicio de entrega"

    # Enviar notificación
    forward_to_whatsapp(contact, message, order_id)


def update_database_schema():
    """Actualiza el esquema de la base de datos si es necesario"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    # Verificar y añadir columnas para manejo de ubicación
    columns_to_add = [
        ("latitude", "REAL"),
        ("longitude", "REAL"),
        ("address_type", "TEXT DEFAULT 'text'"),
        ("restaurant_latitude", "REAL"),
        ("restaurant_longitude", "REAL")
    ]

    try:
        # Verificar si la tabla orders existe
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='orders'")
        if cursor.fetchone():
            # Verificar y añadir columnas necesarias
            for col_name, col_type in columns_to_add:
                try:
                    # Intentar acceder a la columna para ver si existe
                    cursor.execute(f"SELECT {col_name} FROM orders LIMIT 1")
                except sqlite3.OperationalError:
                    # Si la columna no existe, añadirla
                    print(f"Actualizando tabla 'orders': añadiendo columna '{col_name}'")
                    cursor.execute(f"ALTER TABLE orders ADD COLUMN {col_name} {col_type}")
                    conn.commit()

            # Verificar si driver_code existe
            try:
                cursor.execute("SELECT driver_code FROM orders LIMIT 1")
            except sqlite3.OperationalError:
                print("Actualizando tabla 'orders': añadiendo columna 'driver_code'")
                cursor.execute("ALTER TABLE orders ADD COLUMN driver_code TEXT")
                conn.commit()

        # Verificar si la tabla conversations existe
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'")
        if not cursor.fetchone():
            # Crear la tabla para conversaciones
            print("Creando tabla 'conversations' para persistencia de estados")
            cursor.execute('''
            CREATE TABLE conversations (
                phone TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                data TEXT NOT NULL,  -- JSON serializado
                last_updated TEXT DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            conn.commit()

    except Exception as e:
        print(f"Error al actualizar esquema: {e}")

    conn.close()


@app.on_event("startup")
async def startup_db_client():
    update_database_schema()
    init_db()


@app.get("/api")
async def api_root():
    return {
        "service": "Relámpago Express API",
        "version": "1.1",
        "endpoints": {
            "orders": "/api/orders",
            "order": "/api/order/{order_id}",
            "drivers": "/api/drivers",
            "users": "/api/users",
            "location": "/location"
        }
    }


# Endpoint para verificar la salud del sistema
@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "whatsapp_service": WHATSAPP_SERVICE_URL,
        "active_conversations": len(active_conversations)
    }


# Inicialización de drivers como diccionario para acceso rápido
DRIVERS_DICT = {driver["code"]: driver for driver in DRIVERS}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)