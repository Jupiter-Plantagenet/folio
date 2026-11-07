"""No-cache headers for private API responses, without buffering streamed bodies."""


class PrivateApiHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope.get('path', '').startswith('/api/v1/'):
            await self.app(scope, receive, send)
            return
        policy = {
            b'cache-control': b'no-store',
            b'pragma': b'no-cache',
            b'x-content-type-options': b'nosniff',
            b'referrer-policy': b'no-referrer',
        }

        async def private_send(message):
            if message['type'] == 'http.response.start':
                headers = [
                    (name, value)
                    for name, value in message.get('headers', [])
                    if name.lower() not in policy
                ]
                message = {**message, 'headers': headers + list(policy.items())}
            await send(message)

        await self.app(scope, receive, private_send)
