import argparse

from . import AmosCentralClient, ClientConfig, create_private


def main():
    parser = argparse.ArgumentParser(description="Manage an external AMOScentral client identity")
    parser.add_argument("command", choices=["init-key", "connect"])
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = ClientConfig.from_file(args.config)
    if args.command == "init-key":
        create_private(config.state_path(config.private_key_file))
        print("P-256 private key created; back up the external state directory.")
    else:
        with AmosCentralClient(config) as client:
            client.connect()
        print("Address connected and local key verified.")


if __name__ == "__main__":
    main()
