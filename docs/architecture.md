# Arquitectura

El pipeline segueix una arquitectura de quatre zones:

```text
Fonts externes
      |
      v
1.landing-zone/temporal
      |
      v
1.landing-zone/persistent
      |
      v
2.formatted-zone
      |
      v
3.trusted-zone
      |
      v
4.exploitation-zone
```

Cada etapa llegeix exclusivament de la zona anterior i escriu a la zona
seguent. MinIO guarda els objectes; el codi de `src/` executa les
transformacions.

