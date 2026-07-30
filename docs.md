# Ani-Web — dokumentacja architektury i logiki działania

## 1. Cel projektu

Ani-Web ma być lokalnym interfejsem webowym sterującym uruchamianiem i obsługą programu ani-cli na urządzeniach z Android TV lub podobnych systemów, z naciskiem na prostotę, kioskowy tryb działania i działanie z poziomu Termux.

Główne założenia:
- UI działa lokalnie w przeglądarce,
- użytkownik może uruchamiać i kontrolować odtwarzanie anime,
- system zachowuje ustawienia i prostą historię oglądania,
- aplikacja ma działać w środowisku ograniczonym zasobowo, typowo na Android TV,
- VLC ma być domyślnym odtwarzaczem, a jego obsługa ma być prostsza i bardziej stabilna niż pełna integracja z ani-cli.

## 2. Zakres funkcjonalności

### MVP
- prosty interfejs HTML + CSS + JavaScript,
- uruchamianie odtwarzania przez VLC z poziomu lokalnego backendu,
- przekazywanie prostych komend sterujących do VLC,
- wyświetlanie stanu procesu odtwarzania,
- zapis ustawień w przeglądarce (cookies / localStorage),
- prosty ekran listy anime i szczegółów,
- obsługa ponownego uruchamiania w przypadku awarii procesu.

### Rozszerzenia później
- pełna obsługa historii oglądania,
- profil użytkownika,
- cache metadanych anime,
- lepsza obsługa błędów i logów,
- automatyczne wykrywanie i restart procesu.

## 3. Architektura

### 3.1 Warstwy systemu

1. Warstwa interfejsu użytkownika
- statyczne pliki HTML/CSS/JS,
- ekran główny z listą anime,
- ekran ustawień,
- ekran szczegółów anime,
- ekran kioskowy / pełnoekranowy.

2. Warstwa lokalnego backendu
- serwer HTTP uruchamiany lokalnie,
- endpointy do sterowania aplikacją,
- endpointy do odczytu statusu,
- warstwa odpowiedzialna za uruchamianie i zarządzanie procesem ani-cli.

3. Warstwa procesu odtwarzania
- uruchamiany proces VLC jako domyślny odtwarzacz,
- komunikacja odbywa się przez prosty skrypt startowy lub komendy sterujące,
- backend monitoruje stan procesu i reaguje na błędy.

4. Warstwa przechowywania danych
- ustawienia użytkownika w przeglądarce,
- cache metadanych anime lokalnie na urządzeniu,
- opcjonalnie pliki JSON lub prosty lokalny magazyn stanu.

### 3.2 Proponowana organizacja plików

```text
Ani-Web/
  README.md
  docs.md
  deployment.md
  frontend/
    index.html
    styles.css
    app.js
  backend/
    server.py        # lub server.js
    ani_runner.py
    config.json
  data/
    cache/
    settings/
```

## 4. Logika działania

### 4.1 Start aplikacji
1. Użytkownik uruchamia serwer lokalny w Termux.
2. Przeglądarka otwiera lokalny adres HTTP.
3. Frontend ładuje ustawienia z przeglądarki.
4. Backend rozpoczyna pracę w tle i jest gotowy do przyjęcia komend.

### 4.2 Uruchomienie odtwarzania
1. Użytkownik wybiera anime z listy.
2. Frontend wysyła żądanie do backendu: start playback.
3. Backend uruchamia VLC z odpowiednim źródłem lub plikiem anime.
4. Backend zapisuje status procesu i aktualizuje UI.
5. Jeśli proces zakończy się błędem, backend obsługuje restart lub pokazuje komunikat.

### 4.3 Praca w trybie kiosk
- UI ma być maksymalnie proste i skupione na jednym celu: oglądanie anime,
- na ekranie głównym nie powinno być zbędnych elementów,
- ustawienia powinny być dostępne z osobnej strony lub ukryte za prostym mechanizmem.

### 4.4 Cache i metadane
- informacje o anime są pobierane z zewnętrznej strony lub źródła danych,
- dane są przechowywane lokalnie, aby ograniczyć liczbę zapytań,
- przy braku danych system próbuje pobrać je ponownie,
- jeśli nie uda się pobrać, UI pokazuje fallback.

### 4.5 Ustawienia i stan użytkownika
- preferencje użytkownika są przechowywane w ciasteczkach lub localStorage,
- historia oglądania może być zapisana lokalnie w pliku JSON,
- identyfikator użytkownika jest pobierany z ciasteczka lub prostego stanu przeglądarki.

## 5. Komunikacja frontend ↔ backend

### Proponowane endpointy
- GET /api/status — zwraca aktualny stan procesu,
- POST /api/play — uruchamia odtwarzanie,
- POST /api/stop — zatrzymuje proces,
- POST /api/settings — zapisuje ustawienia,
- GET /api/settings — odczytuje ustawienia,
- GET /api/anime — zwraca listę anime lub szczegóły.

## 6. Bezpieczeństwo i ograniczenia

- aplikacja ma działać wyłącznie lokalnie,
- nie jest to projekt przeznaczony do publicznego hostingu,
- warstwa bezpieczeństwa powinna być minimalna,
- nie należy wprowadzać złożonej autoryzacji na tym etapie.

## 7. Zalecenia implementacyjne

- zacząć od prostego backendu w Pythonie, bo dobrze współpracuje z Termux i Androidem,
- frontend utrzymać w czystym JS bez ciężkiego frameworka,
- na start nie wdrażać zbyt wielu zależności,
- najpierw zrealizować działanie podstawowe: start/stop/playback/status,
- VLC ma być pierwszym i domyślnym wariantem odtwarzania,
- dopiero potem dodać cache i historię.

## 8. Plan kolejnych kroków

1. Utworzyć prosty backend serwera lokalnego.
2. Połączyć frontend z endpointami.
3. Dodać uruchamianie VLC.
4. Dodać obsługę stanu procesu.
5. Dodać cache metadanych.
6. Dodać ustawienia i historię oglądania.
7. Przygotować wersję pod Android TV z Termux.
