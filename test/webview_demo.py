import webview


def main():
    window = webview.create_window("Hello pywebview", "https://pywebview.flowrl.com")
    webview.start()


if __name__ == "__main__":
    main()
