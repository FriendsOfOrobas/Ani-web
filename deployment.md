# Ani-Web — plan deploymentu dla Android TV z Termux

## 1. Cel dokumentu

Ten dokument opisuje plan wdrożenia projektu Ani-Web na urządzeniach z Android TV z użyciem Termux, bez wykonywania jeszcze samego deploymentu.

## 2. Zakres

- uruchomienie aplikacji lokalnie na Android TV,
- przygotowanie środowiska Termux,
- uruchamianie backendu i frontendu,
- plan testów na emulatorze lub urządzeniu,
- ograniczenia związane z Android TV i Termux.

## 3. Założenia

- urządzenie ma dostęp do lokalnej sieci lub działa lokalnie,
- użytkownik może otworzyć przeglądarkę na TV lub z innego urządzenia,
- ani-cli jest dostępny w środowisku Termux lub można go zainstalować,
- aplikacja ma działać w prostym, lokalnym modelu bez zewnętrznego hostingu.

## 4. Środowisko Termux

### 4.1 Pakiety potrzebne na start
- python
- git
- curl
- nodejs (opcjonalnie, jeśli frontend będzie wymagał prostego serwera lub narzędzi budowania)
- openssh (opcjonalnie, jeśli będziemy uruchamiać zdalnie)

### 4.2 Przygotowanie środowiska
1. Zainstalować Termux i zaktualizować pakiety.
2. Zainstalować Python i narzędzia wymagane przez projekt.
3. Sklonować repozytorium do katalogu w Termux.
4. Utworzyć wirtualne środowisko Python (jeśli będzie potrzebne).

## 5. Uruchomienie aplikacji

### 5.1 Backend
- uruchomić lokalny serwer z poziomu Termux,
- nasłuchiwać na lokalnym adresie IP lub localhost,
- przygotować prosty start skryptem shellowym.

### 5.2 Frontend
- otworzyć pliki HTML/CSS/JS bezpośrednio w przeglądarce,
- lub uruchomić prosty lokalny serwer statyczny,
- w zależności od tego, czy projekt będzie działał jako pełny stack czy jako prosty interfejs.

### 5.3 Integracja z ani-cli
- sprawdzić, czy ani-cli działa w środowisku Termux,
- przygotować skrypt uruchamiający proces z odpowiednimi argumentami,
- zabezpieczyć obsługę błędów, jeśli proces się zawiesi lub nie startuje.

## 6. Plan testów

### 6.1 Testy lokalne na Linuxie
- sprawdzić działanie backendu,
- sprawdzić komunikację frontend ↔ backend,
- przetestować start/stop procesu.

### 6.2 Testy na Android TV
- uruchomić projekt w Termux na urządzeniu,
- otworzyć aplikację z lokalnej przeglądarki,
- sprawdzić działanie pilota i nawigacji po UI,
- sprawdzić zachowanie po utracie połączenia lub po restarcie procesu.

## 7. Ograniczenia i ryzyka

- Android TV ma ograniczone zasoby i nie zawsze dobrze obsługuje pełne środowiska,
- Termux jest środowiskiem użytkowym, więc stabilność zależy od konfiguracji urządzenia,
- niektóre funkcje ani-cli mogą wymagać dodatkowych zależności lub dostosowania,
- UI musi być zoptymalizowane pod pilot i duży ekran.

## 8. Kolejne kroki po przygotowaniu planu

1. Zainstalować zależności w Termux.
2. Przygotować prosty backend.
3. Połączyć frontend z backendem.
4. Przetestować start procesu ani-cli.
5. Wdrożyć pierwszą wersję lokalną na Android TV.

## 9. Uwagi

Na tym etapie nie uruchamiamy jeszcze deploymentu. Celem jest przygotowanie jasnego planu i kolejnych kroków dla przyszłej produkcji.
