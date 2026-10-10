#!/usr/bin/env bash
set -euo pipefail
IMAGE="${INQUISITOR_JUDGE_IMAGE:-inquisitor-judge-runtime:1.0}"

echo "Inquisitor judge runtime verification"
echo "Image: $IMAGE"

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "ERROR: $IMAGE is not built or not visible to this Docker daemon." >&2
  echo "Build it with: docker build -t $IMAGE -f Dockerfile.judge-runtime ." >&2
  exit 1
}

echo
echo "Compiler/runtime versions:"
docker run --rm "$IMAGE" bash -lc '
set -e
echo "C++:     $(g++ --version | head -n1)"
echo "C:       $(gcc --version | head -n1)"
echo "Java:    $(javac -version 2>&1)"
echo "Kotlin:  $(kotlinc -version 2>&1 | head -n1)"
echo "Python:  $(python3 --version 2>&1)"
echo "Rust:    $(rustc --version 2>&1)"
echo "Ruby:    $(ruby --version 2>&1)"
'

echo
echo "Compile/run smoke tests:"
docker run --rm "$IMAGE" bash -lc '
set -euo pipefail
rm -rf /tmp/inquisitor-lang-test
mkdir -p /tmp/inquisitor-lang-test
cd /tmp/inquisitor-lang-test

cat > main.cpp <<"SRC"
#include <iostream>
int main(){std::cout << 42 << "\n";}
SRC
g++ -std=c++17 -O2 -pipe main.cpp -o cpp.out
[ "$(./cpp.out)" = "42" ] && echo "PASS C++"

cat > main.c <<"SRC"
#include <stdio.h>
int main(void){printf("42\n"); return 0;}
SRC
gcc -std=c17 -O2 -pipe main.c -o c.out
[ "$(./c.out)" = "42" ] && echo "PASS C"

cat > Main.java <<"SRC"
public class Main { public static void main(String[] args){ System.out.println(42); } }
SRC
javac Main.java
[ "$(java Main)" = "42" ] && echo "PASS Java"

cat > Main.kt <<"SRC"
fun main() { println(42) }
SRC
kotlinc Main.kt -include-runtime -d main.jar
[ "$(java -jar main.jar)" = "42" ] && echo "PASS Kotlin"

cat > main.py <<"SRC"
print(42)
SRC
[ "$(python3 -I main.py)" = "42" ] && echo "PASS Python 3"

cat > main.rs <<"SRC"
fn main(){ println!("42"); }
SRC
rustc -O main.rs -o rust.out
[ "$(./rust.out)" = "42" ] && echo "PASS Rust"

cat > main.rb <<"SRC"
puts 42
SRC
[ "$(ruby main.rb)" = "42" ] && echo "PASS Ruby"
'

echo
echo "All seven language smoke tests passed."
