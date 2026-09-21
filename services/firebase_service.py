import os
import json
import firebase_admin
from firebase_admin import credentials, auth, messaging
from firebase_admin.exceptions import FirebaseError

# Initialize Firebase Admin SDK
firebase_initialized = False

# We read the credentials path, project ID, and raw JSON from env variables
firebase_creds_path = os.getenv("FIREBASE_CREDENTIALS_PATH")
firebase_creds_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
firebase_project_id = os.getenv("FIREBASE_PROJECT_ID", "aimer-project1")

if firebase_creds_json:
    try:
        creds_dict = json.loads(firebase_creds_json)
        cred = credentials.Certificate(creds_dict)
        firebase_admin.initialize_app(cred)
        firebase_initialized = True
        print(f"[INFO] Firebase Admin SDK initialized using raw Credentials JSON string.")
    except Exception as e:
        print(f"[ERROR] Failed to initialize Firebase Admin SDK using Credentials JSON: {e}")
elif firebase_creds_path and os.path.exists(firebase_creds_path):
    try:
        cred = credentials.Certificate(firebase_creds_path)
        firebase_admin.initialize_app(cred, options={"projectId": firebase_project_id})
        firebase_initialized = True
        print(f"[INFO] Firebase Admin SDK initialized using Certificate (Project: {firebase_project_id}).")
    except Exception as e:
        print(f"[ERROR] Failed to initialize Firebase Admin SDK using Certificate: {e}")
else:
    # Try default credentials (ADC) or default initialization with explicit project ID
    try:
        firebase_admin.initialize_app(options={"projectId": firebase_project_id})
        firebase_initialized = True
        print(f"[INFO] Firebase Admin SDK initialized (Project: {firebase_project_id}).")
    except Exception as e:
        print(f"[WARNING] Firebase Admin SDK not initialized (no credentials found or invalid): {e}")


def delete_firebase_user(email: str):
    """
    Deletes a user from Firebase Authentication by email.
    Suitable to run as a FastAPI BackgroundTask (runs in thread pool).
    """
    if not firebase_initialized:
        print(f"[WARNING] Skipping Firebase user deletion for {email}: Firebase Admin SDK not initialized.")
        return

    if not email:
        return

    try:
        print(f"[INFO] Attempting to find Firebase user for email: {email}")
        user = auth.get_user_by_email(email)
        auth.delete_user(user.uid)
        print(f"[OK] Successfully deleted user {email} (UID: {user.uid}) from Firebase Authentication.")
    except auth.UserNotFoundError:
        print(f"[INFO] User {email} not found in Firebase Authentication. No deletion needed.")
    except FirebaseError as e:
        print(f"[ERROR] Firebase error when deleting user {email}: {e}")
    except Exception as e:
        print(f"[ERROR] Unexpected error when deleting user {email} from Firebase: {e}")


def send_fcm_data_message(token: str, payload_data: dict) -> bool:
    """
    Sends a data-only FCM push notification to a device token.
    payload_data: key-value string pairs (e.g. {"payload": "..."})
    """
    if not firebase_initialized:
        print("[WARNING] FCM not sent: Firebase Admin SDK not initialized.")
        return False
    try:
        message = messaging.Message(
            data=payload_data,
            token=token,
            android=messaging.AndroidConfig(
                priority="high"
            )
        )
        response = messaging.send(message)
        print(f"[FCM] Successfully sent data message. Response: {response}")
        return True
    except Exception as e:
        print(f"[FCM ERROR] Failed to send data message: {e}")
        return False

