import os
import tempfile

# LARGE 설정은 import 시점에 읽히므로, hub 를 import 하기 전에 테스트용 DB·모드를 정한다.
_tmp = tempfile.mkdtemp(prefix="hub-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/large.db"
os.environ["APP_MODE"] = "demo"
os.environ["EMAIL_MODE"] = "mock"
os.environ["LLM_MODE"] = "mock"
os.environ["HUB_SMALL_SOCKET"] = "off"
os.environ["EMAIL_SCHEDULER"] = "off"  # 테스트 중 LARGE 발송 스케줄러 스레드를 띄우지 않음
