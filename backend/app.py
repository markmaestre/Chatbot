import os
import psycopg2
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
import jwt
import datetime
from dotenv import load_dotenv
import cohere

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
CORS(app, origins=["http://localhost:3000"])

DATABASE_URL = os.getenv('DATABASE_URL')
COHERE_API_KEY = os.getenv("COHERE_API_KEY")
co = cohere.Client(COHERE_API_KEY)

user_memory = {}

def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

# Create user table if it doesn't exist
def create_user_table():
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    password TEXT NOT NULL,
                    history TEXT,
                    last_question TEXT
                )
            ''')
            conn.commit()

create_user_table()

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    hashed_password = generate_password_hash(data['password'])
    email = data['email']
    
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
            user_exists = cursor.fetchone()
            if user_exists:
                return jsonify({"message": "User already exists"}), 400
            
            cursor.execute("INSERT INTO users (email, password, history, last_question) VALUES (%s, %s, %s, %s)", 
                           (email, hashed_password, "", ""))
            conn.commit()

    return jsonify({"message": "User registered successfully"}), 201

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    email = data['email']
    password = data['password']

    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()

            if user and check_password_hash(user[2], password):  # user[2] is the password field
                token = jwt.encode({
                    'user': email,
                    'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=1)
                }, app.config['SECRET_KEY'], algorithm="HS256")
                
                return jsonify({
                    'token': token,
                    'user': {'email': user[1]}  # assuming user[1] is the email field
                }), 200
            return jsonify({"message": "Invalid credentials"}), 401

@app.route('/protected', methods=['GET'])
def protected():
    token = request.headers.get('Authorization').split()[1]
    try:
        jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
        return jsonify({"message": "Access granted"})
    except jwt.ExpiredSignatureError:
        return jsonify({"message": "Token expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"message": "Invalid token"}), 401

@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json.get("message")
    user_email = request.json.get("email")

    if not user_message:
        return jsonify({"error": "No message provided"}), 400

    if user_email not in user_memory:
        user_memory[user_email] = {"name": None, "preferences": [], "history": [], "last_question": None}

    user_memory[user_email]["last_question"] = user_message
    user_memory[user_email]["history"].append(f"User: {user_message}")
    response = handle_message(user_email, user_message)
    user_memory[user_email]["history"].append(f"Bot: {response}")
    save_user_history(user_email, user_message, response)
    return jsonify({"response": response})

def handle_message(user_email, user_message):
    user_data = user_memory[user_email]
    name = user_data.get("name")

    if is_tagalog(user_message):
        return handle_tagalog_response(user_email, user_message)

    if "good morning" in user_message.lower():
        return f"Good morning {name}!" if name else "Good morning! How can I assist you today?"
    
    elif "hello" in user_message.lower():
        return f"Hello {name}!" if name else "Hi there! What's your name?"

    elif "bye" in user_message.lower():
        return "Goodbye! Have a great day."

    elif "my name is" in user_message.lower():
        name = user_message.split("my name is")[-1].strip()
        user_data["name"] = name
        return f"Got it, {name}! I will remember your name."

    elif "preferences" in user_message.lower():
        return f"Your preferences: {', '.join(user_data['preferences'])}"

    return call_cohere_api(user_message)

def is_tagalog(message):
    tagalog_keywords = ['kamusta', 'magandang araw', 'salamat', 'paalam', 'kumusta', 'oo', 'hindi']
    return any(keyword in message.lower() for keyword in tagalog_keywords)

def handle_tagalog_response(user_email, user_message):
    return "Pasensya na, hindi ko masyadong maintindihan. Puwede mo bang ulitin?"

def call_cohere_api(user_message):
    try:
        response = co.generate(
            model='command',
            prompt=user_message,
            max_tokens=100
        )
        return response.generations[0].text.strip()
    except Exception as e:
        return "Error: Cannot process your request at the moment."

def save_user_history(user_email, user_message, bot_response):
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE email = %s", (user_email,))
            user_data = cursor.fetchone()
            if user_data:
                history = user_data[3]
                updated_history = history + f"User: {user_message} | Bot: {bot_response}\n"
                cursor.execute("UPDATE users SET history = %s WHERE email = %s", (updated_history, user_email))
                conn.commit()

if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=5000)
