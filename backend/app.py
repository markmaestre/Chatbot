import os
import psycopg2
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
import jwt
import datetime
from dotenv import load_dotenv
import cohere
import requests
from psycopg2 import sql

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
CORS(app, origins=["http://localhost:3000"])

# Retrieve database URL from environment variable
DATABASE_URL = os.getenv('DATABASE_URL')

# Initialize Cohere API client
COHERE_API_KEY = os.getenv("COHERE_API_KEY")
co = cohere.Client(COHERE_API_KEY)

# In-memory user data (for temporary memory of user)
user_memory = {}

# Function to create a database connection
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"Error connecting to database: {e}")
        return None

# Create user table if it doesn't exist
def create_user_table():
    conn = get_db_connection()
    if conn is not None:
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
        conn.close()

# Register route
@app.route('/register', methods=['POST'])
def register():
    data = request.json
    hashed_password = generate_password_hash(data['password'])
    email = data['email']

    conn = get_db_connection()
    if conn is not None:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
            user_exists = cursor.fetchone()
            if user_exists:
                conn.close()
                return jsonify({"message": "User already exists"}), 400

            cursor.execute("INSERT INTO users (email, password, history, last_question) VALUES (%s, %s, %s, %s)", 
                           (email, hashed_password, "", ""))
            conn.commit()
        conn.close()

    return jsonify({"message": "User registered successfully"}), 201

# Login route
@app.route('/login', methods=['POST'])
def login():
    data = request.json
    email = data['email']
    password = data['password']

    conn = get_db_connection()
    if conn is not None:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()

            if user and check_password_hash(user[2], password):  # user[2] is the password field
                token = jwt.encode({
                    'user': email,
                    'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=1)
                }, app.config['SECRET_KEY'], algorithm="HS256")

                # Include user email in the response for frontend to use
                conn.close()
                return jsonify({
                    'token': token,
                    'user': {
                        'email': user[1]  # assuming user[1] is the email field
                    }
                }), 200
            conn.close()
            return jsonify({"message": "Invalid credentials"}), 401

# Protected route
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

# Health check route
@app.route('/health', methods=['GET'])
def health_check():
    try:
        conn = get_db_connection()
        if conn is not None:
            conn.close()
            return jsonify({"message": "Database is running"}), 200
        else:
            return jsonify({"message": "Database connection failed"}), 500
    except Exception as e:
        return jsonify({"message": f"Error connecting to the database: {str(e)}"}), 500

# Chat route using Cohere
@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json.get("message")
    user_email = request.json.get("email")  # Use email to identify users in memory

    if not user_message:
        return jsonify({"error": "No message provided"}), 400

    # Initialize user memory if not already stored
    if user_email not in user_memory:
        user_memory[user_email] = {"name": None, "preferences": [], "history": [], "last_question": None}

    # Save the current message as the user's last question
    user_memory[user_email]["last_question"] = user_message

    # Add the current message to the user's history
    user_memory[user_email]["history"].append(f"User: {user_message}")

    # Respond based on user's memory or intent
    response = handle_message(user_email, user_message)

    # Save the bot's response to history
    user_memory[user_email]["history"].append(f"Bot: {response}")

    # Save user history to PostgreSQL
    save_user_history(user_email, user_message, response)

    return jsonify({"response": response})

# Handle the user's message and respond accordingly
def handle_message(user_email, user_message):
    user_data = user_memory[user_email]
    name = user_data.get("name")

    # Detect if the user is speaking in Tagalog based on keywords
    if is_tagalog(user_message):
        return handle_tagalog_response(user_email, user_message)

    # Check for Good Morning / Good Evening
    if "good morning" in user_message.lower():
        response = "Good morning! How can I assist you today?"
        if name:
            response = f"Good morning {name}! How can I assist you today?"
    
    elif "good evening" in user_message.lower():
        response = "Good evening! How can I assist you tonight?"
        if name:
            response = f"Good evening {name}! How can I assist you tonight?"
    
    # Default English responses
    elif "hello" in user_message.lower():
        response = "Hi there! How can I help you today?"
        if name:
            response = f"Hello {name}! How can I assist you today?"
        elif name is None:
            response = "Hi there! What's your name?"
    
    elif "bye" in user_message.lower():
        response = "Goodbye! Have a great day."

    elif "my name is" in user_message.lower():
        name = user_message.lower().split("my name is")[-1].strip()
        user_data["name"] = name
        response = f"Got it, {name}! I will remember your name."

    elif "preferences" in user_message.lower():
        response = f"Your current preferences are: {', '.join(user_data['preferences'])}"

    elif "history" in user_message.lower():
        history = user_data["history"]
        if history:
            response = "Here are your previous messages:\n" + "\n".join(history)
        else:
            response = "No history available."
    
    elif "last question" in user_message.lower():
        last_question = user_data.get("last_question", "No questions yet.")
        response = f"Your last question was: {last_question}"

    else:
        response = call_cohere_api(user_message)

    return response

# Check if the user's message is in Tagalog
def is_tagalog(message):
    tagalog_keywords = ['kamusta', 'magandang araw', 'salamat', 'paalam', 'kumusta', 'oo', 'hindi']
    return any(keyword in message.lower() for keyword in tagalog_keywords)

# Handle responses in Tagalog
def handle_tagalog_response(user_email, user_message):
    user_data = user_memory[user_email]
    name = user_data.get("name")

    if "kamusta" in user_message.lower() or "kumusta" in user_message.lower():
        response = "Kamusta! Paano kita matutulungan ngayon?"
        if name:
            response = f"Kamusta {name}! Paano kita matutulungan?"
        elif name is None:
            response = "Kamusta! Anong pangalan mo?"

    elif "magandang araw" in user_message.lower():
        response = "Magandang araw! Ano ang maitutulong ko sa iyo?"

    elif "salamat" in user_message.lower():
        response = "Walang anuman! Nandito lang ako kung kailangan mo ako."

    elif "paalam" in user_message.lower():
        response = "Paalam! Magandang araw sa iyo."

    elif "oo" in user_message.lower():
        response = "Tama, oo nga!"

    elif "hindi" in user_message.lower():
        response = "Ayos lang, walang problema."

    else:
        response = "Pasensya na, hindi ko masyadong maintindihan. Puwede mo bang ulitin?"

    return response

# Call Cohere API
def call_cohere_api(user_message):
    try:
        response = co.generate(
            model='xlarge',
            prompt=user_message,
            max_tokens=100,
            temperature=0.7
        )
        return response.generations[0].text.strip()
    except Exception as e:
        print(f"Error calling Cohere API: {e}")
        return "Sorry, I encountered an issue processing your message."

# Save user history to PostgreSQL
def save_user_history(email, question, response):
    conn = get_db_connection()
    if conn is not None:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE users SET history = history || %s WHERE email = %s",
                (f"User: {question}\nBot: {response}", email)
            )
            conn.commit()
        conn.close()

# Initialize the user table on startup and check database connection
conn = get_db_connection()
if conn is not None:
    print("Successfully connected to the database!")
    create_user_table()
    conn.close()
else:
    print("Error connecting to the database.")

if __name__ == '__main__':
    app.run(debug=True)
