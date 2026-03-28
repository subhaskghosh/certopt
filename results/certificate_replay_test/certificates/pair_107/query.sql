SELECT
  p.firstname,
  p.lastname,
  (
    SELECT
      CITY
    FROM ADDRESS
    WHERE
      PERSONID = P.PERSONID
  ) AS CITY,
  (
    SELECT
      STATE
    FROM ADDRESS
    WHERE
      PERSONID = P.PERSONID
  ) AS STATE
FROM PERSON AS P