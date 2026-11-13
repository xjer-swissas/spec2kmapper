// Compatibility harness against the supplied amc-message-builder-1.3.1.jar.
// No proprietary Java implementation is copied into this repository.
import java.io.*;
import java.lang.reflect.Proxy;
import java.nio.file.*;
import java.security.*;
import java.security.cert.Certificate;
import java.security.cert.CertificateFactory;
import java.security.spec.PKCS8EncodedKeySpec;
import amos.central.client.message.api.*;
import amos.central.client.message.api.keystore.AmosCentralKeystore;
import amos.central.client.message.api.model.AmosCentralMessage;
import amos.central.client.message.internal.converter.AmosCentralRestMessageConverter;
import amos.central.client.message.internal.encryption.AmosCentralEncoderProcess;

public class JavaMessageInterop {
    public static void main(String[] args) throws Exception {
        PrivateKey privateKey = KeyFactory.getInstance("EC").generatePrivate(
            new PKCS8EncodedKeySpec(Files.readAllBytes(Path.of(args[1]))));
        Certificate certificate;
        try (InputStream in = Files.newInputStream(Path.of(args[2]))) {
            certificate = CertificateFactory.getInstance("X.509").generateCertificate(in);
        }
        AmosCentralKeystore store = (AmosCentralKeystore) Proxy.newProxyInstance(
            JavaMessageInterop.class.getClassLoader(), new Class[]{AmosCentralKeystore.class},
            (proxy, method, arguments) -> switch (method.getName()) {
                case "getActivePrivateKey" -> privateKey;
                case "getCertificateForAddress" -> certificate;
                default -> null;
            });
        AmosCentralMessageLogger logger = (AmosCentralMessageLogger) Proxy.newProxyInstance(
            JavaMessageInterop.class.getClassLoader(), new Class[]{AmosCentralMessageLogger.class},
            (proxy, method, arguments) -> null);
        AmosCentralRestCall rest = (AmosCentralRestCall) Proxy.newProxyInstance(
            JavaMessageInterop.class.getClassLoader(), new Class[]{AmosCentralRestCall.class},
            (proxy, method, arguments) -> method.getName().equals("isChannel") ? false : certificate);
        AmosCentralMessage message;
        try (InputStream in = Files.newInputStream(Path.of(args[3]))) {
            message = AmosCentralRestMessageConverter.fromStream(in);
            AmosCentralEncoderProcess process = new AmosCentralEncoderProcess(store, rest, false);
            if (args[0].equals("encode")) {
                if (args.length > 5 && args[5].equals("gzip")) message.getProperties().put("to_compress", "true");
                process.encode(message, logger);
            } else {
                process.performDecryption(message, certificate, privateKey, logger);
            }
            try (OutputStream out = Files.newOutputStream(Path.of(args[4]))) {
                AmosCentralRestMessageConverter.toStream(message, out);
            }
        }
    }
}
