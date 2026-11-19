"""비밀값 읽기. 환경 변수에서만 읽고 값은 출력하지 않는다.

로컬에서는 .env를 환경 변수로 읽어 들인 뒤 실행하고(README 참고), 배포 환경에서는 Kubernetes Secret 등으로 주입한다.
"""
import os


class MissingSecret(Exception):
    pass


def secret(name):
    value = os.environ.get(name)
    if value:
        return value
    raise MissingSecret(f"{name} 환경 변수가 없음")
