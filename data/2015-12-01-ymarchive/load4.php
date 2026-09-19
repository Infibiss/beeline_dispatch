<?
  include "mysql.php";
  // Read traffic images from iprobki.ru
  $ta = array(0 => array(186, 178),
              1 => array(199, 124),
              2 => array(200, 117),
              3 => array(200, 128),
              4 => array(180, 251),
              5 => array(186, 251),
              6 => array(187, 250),
              7 => array(110, 240),
              8 => array(107, 235),
              9 => array(112, 239),
              10 => array(96, 237),
              );
  // Analyze iprobki.ru images
  $y2 = "";
  for ($y=2013; $y<2016; $y++) {
    for ($m=1; $m<13; $m++) {
      $m2 = str_pad($m, 2, "0", STR_PAD_LEFT);
      for ($d=31; $d<32; $d++) {
        $d2 = str_pad($d, 2, "0", STR_PAD_LEFT);
        for ($h=0; $h<24; $h++) {
          $h2 = str_pad($h, 2, "0", STR_PAD_LEFT);
          $fname = "cache2/$y-$m2-$d2-$h2-00.gif";
          $tileImg = imagecreatefrompng($fname);
          if ($tileImg === false) continue;
          $ca = array();
          for ($x=22; $x<45; $x++) {
            for ($z=15; $z<40; $z++) {
              $ca[] = imagecolorsforindex($tileImg, imagecolorat($tileImg, $x, $z));
            }
          }
          //print_r($ca);
          // Make average
          $cav = array();
          $count = 0;
          foreach ($ca as $key => $val) {
            if ($val[blue] != 255) {
              $count++;
              $cav[green] += $val[green];
              $cav[red] += $val[red];
            }
          }
          $cav[green] = round($cav[green]/$count);
          $cav[red] = round($cav[red]/$count);
          // Find closest
          $cav2 = array();
          foreach ($ta as $key2 => $val2) {
            //echo "Compare $cav[green] to $val2[0] and $cav[red] to $val2[1]<br>";
            $cav2[$key2] = abs($cav[green]-$val2[0]) + abs($cav[red]-$val2[1]);
          }
          asort($cav2);
          foreach ($cav2 as $key => $val) {
            $traf = $key;
            break;
          }
          //echo "$fname $cav[green] $cav[red] $traf <img src=$fname><br>";
          imagedestroy($tileImg);
          mysql_query("UPDATE hours SET traf='$traf' WHERE 
            YEAR(hour)='$y' AND MONTH(hour)='$m' AND DAY(hour)='$d' AND HOUR(hour)='$h'
          ");
          echo mysql_error();
        }
        //if ($d>10) exit;
      }
    }
  }
?>