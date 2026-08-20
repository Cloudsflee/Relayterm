package com.relayterm.app;

import android.util.Base64;

import java.nio.charset.StandardCharsets;
import java.security.KeyStore;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Encrypts profile tokens without adding an external crypto dependency. */
public final class SecretStore {
    private static final String STORE = "AndroidKeyStore";
    private static final String ALIAS = "relayterm_profile_token";
    private static final String PREFIX = "v1:";
    public SecretStore() { }

    private SecretKey key() throws Exception {
        KeyStore store = KeyStore.getInstance(STORE);
        store.load(null);
        if (store.containsAlias(ALIAS)) {
            return ((KeyStore.SecretKeyEntry) store.getEntry(ALIAS, null)).getSecretKey();
        }
        KeyGenerator generator = KeyGenerator.getInstance("AES", STORE);
        generator.init(new android.security.keystore.KeyGenParameterSpec.Builder(
                ALIAS,
                android.security.keystore.KeyProperties.PURPOSE_ENCRYPT
                        | android.security.keystore.KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(android.security.keystore.KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(android.security.keystore.KeyProperties.ENCRYPTION_PADDING_NONE)
                .build());
        return generator.generateKey();
    }

    public String encrypt(String plain) {
        if (plain == null || plain.isEmpty()) return "";
        try {
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.ENCRYPT_MODE, key());
            byte[] iv = cipher.getIV();
            byte[] encrypted = cipher.doFinal(plain.getBytes(StandardCharsets.UTF_8));
            byte[] packed = new byte[iv.length + encrypted.length];
            System.arraycopy(iv, 0, packed, 0, iv.length);
            System.arraycopy(encrypted, 0, packed, iv.length, encrypted.length);
            return PREFIX + Base64.encodeToString(packed, Base64.NO_WRAP);
        } catch (Exception ignored) {
            // A missing keystore should not make the profile list unusable.
            return "";
        }
    }

    public String decrypt(String encoded) {
        if (encoded == null || encoded.isEmpty()) return "";
        // Read pre-keystore fixtures as plain text so upgrades do not lose profiles.
        if (!encoded.startsWith(PREFIX)) return encoded;
        try {
            byte[] packed = Base64.decode(encoded.substring(PREFIX.length()), Base64.NO_WRAP);
            byte[] iv = new byte[12];
            byte[] ciphertext = new byte[packed.length - iv.length];
            System.arraycopy(packed, 0, iv, 0, iv.length);
            System.arraycopy(packed, iv.length, ciphertext, 0, ciphertext.length);
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, iv));
            return new String(cipher.doFinal(ciphertext), StandardCharsets.UTF_8);
        } catch (Exception ignored) {
            return "";
        }
    }
}
